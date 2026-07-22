# vim: filetype=python
"""Daemon-side launcher for local background agent sessions.

Implements the spawn half of docs/specs/2026-07-21-local-agent-spawn.md: an
opt-in, workstation-only capability that lets the daemon dispatch a new
background Claude Code session (`claude --bg`) seeded with a caller-supplied
initial message. The daemon only launches — there is no supervision loop; the
operator pins sessions they want alive past the harness's idle settle.

Security posture (all load-bearing, see the spec):
  - OFF unless NOTIFICATIONS_SPAWN_ENABLED is set, and never in a managed
    container context (KUBERNETES_SERVICE_HOST belt).
  - The child working directory must resolve (realpath) under one of the
    operator-configured NOTIFICATIONS_SPAWN_ROOTS.
  - Fixed argv template: the initial message is a single argv element after an
    explicit `--` end-of-options marker, never interpreted by a shell and never
    parseable as a flag.
  - Concurrency cap + cooldown bound the blast radius of spawn loops.

Active-session accounting intersects the shorts this daemon recorded spawning
(spawned.json) with the live sessions reported by `claude agents --json` — job
dirs under ~/.claude/jobs/ are history, not liveness, and are never consulted.

stdlib only.
"""

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_ACTIVE = 4
DEFAULT_COOLDOWN_SECONDS = 30.0
DEFAULT_CHANNEL_SPEC = "plugin:notifications@wlr-cc-plugins"
# `claude --bg` prints its banner and exits well under this; a hang here means
# the harness never dispatched and the child should be considered failed.
DISPATCH_TIMEOUT_SECONDS = 120.0

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# First line of a successful dispatch: "backgrounded · <short> ..." (the
# separator may be lost or re-encoded depending on the stream, so it is
# optional and the short is the first non-space token after it).
_BACKGROUNDED = re.compile(r"backgrounded\s*[·\xb7]?\s*(\S+)")


class SpawnError(Exception):
    """A spawn request refused or failed; str(err) is the client-facing reason."""


@dataclass(frozen=True)
class SpawnConfig:
    enabled: bool
    disabled_reason: str | None
    roots: tuple[Path, ...]
    max_active: int
    cooldown_seconds: float
    claude_bin: str
    channel_spec: str
    prewarm_scripts: tuple[Path, ...]


def _default_prewarm_scripts() -> tuple[Path, ...]:
    """The relay + session-start hook, whose uv envs must be warm before the
    child's MCP connect (a cold first `uv run` can miss the harness's connect
    timeout, which is never retried for the session's lifetime)."""
    plugin_root = Path(__file__).resolve().parent.parent
    return (
        plugin_root / "mcp" / "notifications-server.py",
        plugin_root / "hooks" / "session-start.py",
    )


def load_config(env: Mapping[str, str]) -> SpawnConfig:
    """Resolve spawn configuration from the environment. Always returns a
    config; when spawn is unavailable, `enabled` is False and
    `disabled_reason` says why (the client-facing error text)."""
    roots = tuple(
        Path(part)
        for part in (env.get("NOTIFICATIONS_SPAWN_ROOTS") or "").split(":")
        if part
    )

    disabled_reason: str | None = None
    if not env.get("NOTIFICATIONS_SPAWN_ENABLED"):
        disabled_reason = (
            "spawn is not enabled on this daemon (NOTIFICATIONS_SPAWN_ENABLED)"
        )
    elif env.get("KUBERNETES_SERVICE_HOST"):
        disabled_reason = (
            "spawn is disabled: this daemon is running in a managed container "
            "context; provision agents through the deployment config instead"
        )
    elif not roots:
        disabled_reason = (
            "spawn is enabled but no NOTIFICATIONS_SPAWN_ROOTS are configured"
        )

    def _float(name: str, default: float) -> float:
        raw = env.get(name)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        return default

    prewarm_raw = env.get("NOTIFICATIONS_SPAWN_PREWARM")
    if prewarm_raw is None:
        prewarm = _default_prewarm_scripts()
    else:
        prewarm = tuple(Path(part) for part in prewarm_raw.split(":") if part)

    return SpawnConfig(
        enabled=disabled_reason is None,
        disabled_reason=disabled_reason,
        roots=roots,
        max_active=int(_float("NOTIFICATIONS_SPAWN_MAX_ACTIVE", DEFAULT_MAX_ACTIVE)),
        cooldown_seconds=_float(
            "NOTIFICATIONS_SPAWN_COOLDOWN_SECONDS", DEFAULT_COOLDOWN_SECONDS
        ),
        claude_bin=env.get("NOTIFICATIONS_SPAWN_CLAUDE_BIN") or "claude",
        channel_spec=env.get("NOTIFICATIONS_SPAWN_CHANNELS") or DEFAULT_CHANNEL_SPEC,
        prewarm_scripts=prewarm,
    )


def validate_working_dir(config: SpawnConfig, requested: str) -> Path:
    """Resolve and allowlist-check a requested working directory.

    Realpath resolution happens before the containment check so `..` segments
    and symlinks cannot escape a root.
    """
    if not requested:
        raise SpawnError("working_dir is required")
    candidate = Path(requested)
    if not candidate.is_absolute():
        raise SpawnError("working_dir must be an absolute path")
    resolved = candidate.resolve()
    if not resolved.is_dir():
        raise SpawnError(f"working_dir does not exist: {requested}")
    for root in config.roots:
        root_resolved = root.resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved
    raise SpawnError(f"working_dir {requested} is not under any configured spawn root")


class SpawnState:
    """Persisted record of what this daemon spawned: the shorts (for active
    accounting) and the last spawn time (so the cooldown survives restarts)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.last_spawn_at: float = 0.0
        self.spawned: list[dict[str, object]] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            try:
                self.last_spawn_at = float(data.get("last_spawn_at") or 0.0)
            except (TypeError, ValueError):
                self.last_spawn_at = 0.0
            entries = data.get("spawned")
            if isinstance(entries, list):
                self.spawned = [e for e in entries if isinstance(e, dict)]

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"last_spawn_at": self.last_spawn_at, "spawned": self.spawned}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self._path)

    def shorts(self) -> set[str]:
        return {str(e.get("short")) for e in self.spawned if e.get("short")}

    def record(self, entry: dict[str, object], now: float) -> None:
        self.last_spawn_at = now
        self.spawned.append(entry)
        self._save()

    def touch(self, now: float) -> None:
        """Start the cooldown clock even when the dispatch itself failed, so a
        crash-looping caller cannot retry launch attempts at full speed."""
        self.last_spawn_at = now
        self._save()


def parse_agents_json(text: str) -> set[str]:
    """Short ids of live sessions from `claude agents --json` output."""
    try:
        items = json.loads(text)
    except ValueError as exc:
        raise SpawnError(f"could not parse `claude agents --json` output: {exc}")
    shorts: set[str] = set()
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                shorts.add(str(item["id"]))
    return shorts


def parse_backgrounded_short(output: str) -> str | None:
    """Extract the new session's short id from `claude --bg` stdout."""
    match = _BACKGROUNDED.search(_ANSI.sub("", output))
    return match.group(1) if match else None


def check_gates(
    config: SpawnConfig,
    state: SpawnState,
    live_shorts: set[str],
    now: float,
) -> None:
    """Raise SpawnError if cooldown or the active cap refuse this spawn."""
    elapsed = now - state.last_spawn_at
    if state.last_spawn_at and elapsed < config.cooldown_seconds:
        wait = config.cooldown_seconds - elapsed
        raise SpawnError(f"spawn cooldown: try again in {wait:.0f}s")
    active = state.shorts() & live_shorts
    if len(active) >= config.max_active:
        raise SpawnError(
            f"spawn cap reached: {len(active)} daemon-spawned sessions are "
            f"still active (max {config.max_active})"
        )


async def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float,
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SpawnError(f"{argv[0]} timed out after {timeout:.0f}s")
    return proc.returncode or 0, out.decode(errors="replace")


async def list_live_shorts(config: SpawnConfig) -> set[str]:
    code, out = await _run(
        [config.claude_bin, "agents", "--json"],
        timeout=DISPATCH_TIMEOUT_SECONDS,
    )
    if code != 0:
        raise SpawnError(f"`claude agents --json` failed (exit {code})")
    return parse_agents_json(out)


async def prewarm(config: SpawnConfig) -> list[str]:
    """Warm the uv environments the child's relay/hooks will run from.

    Best-effort: a failure is reported (for the audit trail) but does not block
    the spawn — the child may still connect in time on a warm machine.
    """
    problems: list[str] = []
    for script in config.prewarm_scripts:
        if not script.is_file():
            problems.append(f"prewarm target missing: {script}")
            continue
        try:
            code, _ = await _run(
                ["uv", "sync", "--script", str(script)],
                timeout=DISPATCH_TIMEOUT_SECONDS,
            )
        except (OSError, SpawnError) as exc:
            problems.append(f"prewarm failed for {script.name}: {exc}")
            continue
        if code != 0:
            problems.append(f"prewarm failed for {script.name} (exit {code})")
    return problems


def build_argv(
    config: SpawnConfig, initial_message: str, name: str | None
) -> list[str]:
    """The fixed dispatch template. The initial message rides after `--` so a
    leading-dash message cannot be parsed as a flag by the harness."""
    argv = [config.claude_bin, "--bg"]
    if name:
        argv += ["--name", name]
    argv += ["--dangerously-load-development-channels", config.channel_spec]
    argv += ["--", initial_message]
    return argv


def build_child_env(
    base_env: Mapping[str, str],
    *,
    ws_url: str,
    token: str,
    name: str | None,
) -> dict[str, str]:
    env = dict(base_env)
    env["NOTIFICATIONS_WS_URL"] = ws_url
    env["NOTIFICATIONS_TOKEN"] = token
    # Headless dispatch would otherwise hit an interactive daemon cold-start path.
    env["CLAUDE_CODE_DAEMON_COLD_START"] = "transient"
    if name:
        # Per-child reclaim key so a restarted agent can re-take its own name;
        # never inherit a shared key (a sibling sharing it can steal a live
        # agent's name during a reconnect blip).
        env["NOTIFICATIONS_RECLAIM_KEY"] = name
    else:
        env.pop("NOTIFICATIONS_RECLAIM_KEY", None)
    return env


async def dispatch(
    config: SpawnConfig,
    *,
    initial_message: str,
    working_dir: Path,
    name: str | None,
    child_env: dict[str, str],
) -> tuple[str, str]:
    """Launch the session; return (short, raw dispatch output)."""
    argv = build_argv(config, initial_message, name)
    try:
        code, out = await _run(
            argv, cwd=working_dir, env=child_env, timeout=DISPATCH_TIMEOUT_SECONDS
        )
    except OSError as exc:
        raise SpawnError(f"could not launch {config.claude_bin}: {exc}")
    if code != 0:
        raise SpawnError(
            f"dispatch failed (exit {code}): {out.strip()[:500] or 'no output'}"
        )
    short = parse_backgrounded_short(out)
    if not short:
        raise SpawnError(
            "dispatch produced no session id (no 'backgrounded' banner in "
            f"output): {out.strip()[:500] or 'no output'}"
        )
    return short, out


def audit_record(
    *,
    requester_session: str,
    requester_name: str | None,
    working_dir: str,
    name: str | None,
    outcome: str,
    short: str | None,
    now: float,
) -> dict[str, object]:
    return {
        "ts": now,
        "requester_session": requester_session,
        "requester_name": requester_name,
        "working_dir": working_dir,
        "name": name,
        "outcome": outcome,
        "short": short,
    }


def append_audit(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record) + "\n")
