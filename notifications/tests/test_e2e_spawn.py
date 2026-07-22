# vim: filetype=python
"""End-to-end tests for local agent spawn: real daemon + relay, with `claude`
replaced by a fake binary (NOTIFICATIONS_SPAWN_CLAUDE_BIN) that records its
argv/env/cwd and prints the harness's `backgrounded · <short>` banner. Proves
the whole vertical — tool call → daemon gates → dispatch template → result —
without ever launching a real session."""

import json
import textwrap
from pathlib import Path

import anyio
import pytest

import _harness as h

pytestmark = pytest.mark.slow

FAKE_SHORT = "fake1234"


def _write_fake_claude(tmp_path: Path, log: Path) -> Path:
    """A stand-in `claude` binary: `agents --json` reports no live sessions;
    `--bg` records the full invocation for assertions and prints the banner."""
    script = tmp_path / "fake-claude"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, os, sys

            if sys.argv[1:3] == ["agents", "--json"]:
                print("[]")
                sys.exit(0)
            record = {{
                "argv": sys.argv[1:],
                "cwd": os.getcwd(),
                "env": {{
                    k: os.environ.get(k)
                    for k in (
                        "NOTIFICATIONS_WS_URL",
                        "NOTIFICATIONS_TOKEN",
                        "NOTIFICATIONS_RECLAIM_KEY",
                        "CLAUDE_CODE_DAEMON_COLD_START",
                    )
                }},
            }}
            with open({str(log)!r}, "a") as f:
                f.write(json.dumps(record) + "\\n")
            print("backgrounded \\xb7 {FAKE_SHORT}")
            """
        )
    )
    script.chmod(0o755)
    return script


def _spawn_daemon_env(ws: int, store: Path, tmp_path: Path, fake_claude: Path) -> dict:
    env = h.daemon_env(ws, store)
    env["NOTIFICATIONS_SPAWN_ENABLED"] = "1"
    env["NOTIFICATIONS_SPAWN_ROOTS"] = str(tmp_path / "work")
    env["NOTIFICATIONS_SPAWN_CLAUDE_BIN"] = str(fake_claude)
    env["NOTIFICATIONS_SPAWN_PREWARM"] = ""  # no uv sync in tests
    env["NOTIFICATIONS_SPAWN_COOLDOWN_SECONDS"] = "9999"  # asserted below
    return env


def test_spawn_vertical_and_gates(tmp_path):
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    work = tmp_path / "work" / "proj"
    outside = tmp_path / "elsewhere"
    for d in (store, xdg, work, outside):
        d.mkdir(parents=True)
    log = tmp_path / "fake-claude.jsonl"
    fake_claude = _write_fake_claude(tmp_path, log)
    ws = h.free_port()

    with h.daemon_process(_spawn_daemon_env(ws, store, tmp_path, fake_claude)):

        async def scenario():
            async with h.agent_session(tmp_path, ws, store, xdg, "sid-spawner") as (
                read,
                write,
            ):
                # Spawn requires a registered agent (audit trail carries a name).
                text, _ = await h.mcp_call(
                    read,
                    write,
                    2,
                    "spawn_agent",
                    {"initial_message": "hi", "working_dir": str(work)},
                )
                assert "register_agent first" in text

                await h.mcp_call(read, write, 3, "register_agent", {"name": "spawner"})

                # cwd allowlist enforced
                text, _ = await h.mcp_call(
                    read,
                    write,
                    4,
                    "spawn_agent",
                    {"initial_message": "hi", "working_dir": str(outside)},
                )
                assert "not under" in text

                # the real thing — leading-dash message must ride as data
                text, _ = await h.mcp_call(
                    read,
                    write,
                    5,
                    "spawn_agent",
                    {
                        "initial_message": "--evil looking message",
                        "working_dir": str(work),
                        "name": "helper",
                    },
                )
                assert FAKE_SHORT in text
                assert "helper" in text

                # cooldown gate refuses an immediate second spawn
                text, _ = await h.mcp_call(
                    read,
                    write,
                    6,
                    "spawn_agent",
                    {"initial_message": "again", "working_dir": str(work)},
                )
                assert "cooldown" in text

        anyio.run(scenario)

    # Dispatch template assertions from the fake binary's recording.
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["cwd"] == str(work.resolve())
    argv = record["argv"]
    assert argv[0] == "--bg"
    assert argv[argv.index("--name") + 1] == "helper"
    assert "--dangerously-load-development-channels" in argv
    assert argv[-2:] == ["--", "--evil looking message"]
    env = record["env"]
    assert env["NOTIFICATIONS_WS_URL"] == f"ws://127.0.0.1:{ws}"
    assert env["NOTIFICATIONS_TOKEN"]
    assert env["NOTIFICATIONS_RECLAIM_KEY"] == "helper"
    assert env["CLAUDE_CODE_DAEMON_COLD_START"] == "transient"

    # Audit trail: one refused (bad cwd), one refused (cooldown), one spawned.
    audit_lines = [
        json.loads(line)
        for line in (store / "spawn" / "audit.jsonl").read_text().splitlines()
    ]
    outcomes = [entry["outcome"] for entry in audit_lines]
    assert sum(1 for o in outcomes if o.startswith("refused")) == 2
    assert any(o == "spawned" for o in outcomes)
    spawned = next(e for e in audit_lines if e["outcome"] == "spawned")
    assert spawned["requester_name"] == "spawner"
    assert spawned["short"] == FAKE_SHORT

    # Spawn state persisted for active accounting across restarts.
    state = json.loads((store / "spawn" / "spawned.json").read_text())
    assert [e["short"] for e in state["spawned"]] == [FAKE_SHORT]


def test_spawn_disabled_daemon_refuses(tmp_path):
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    work = tmp_path / "work"
    for d in (store, xdg, work):
        d.mkdir(parents=True)
    ws = h.free_port()

    # Plain daemon env: no NOTIFICATIONS_SPAWN_ENABLED.
    with h.daemon_process(h.daemon_env(ws, store)):

        async def scenario():
            async with h.agent_session(tmp_path, ws, store, xdg, "sid-x") as (
                read,
                write,
            ):
                await h.mcp_call(read, write, 2, "register_agent", {"name": "probe"})
                text, _ = await h.mcp_call(
                    read,
                    write,
                    3,
                    "spawn_agent",
                    {"initial_message": "hi", "working_dir": str(work)},
                )
                assert "not enabled" in text

        anyio.run(scenario)
