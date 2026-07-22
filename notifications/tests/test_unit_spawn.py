# vim: filetype=python
"""Unit tests for lib/agent_spawn.py: config gating, cwd allowlisting, dispatch
output parsing, active accounting, and the persisted spawn state."""

import json

import pytest

import agent_spawn


# --- load_config -----------------------------------------------------------


def test_config_disabled_by_default():
    config = agent_spawn.load_config({})
    assert not config.enabled
    assert "not enabled" in (config.disabled_reason or "")


def test_config_k8s_belt_overrides_enable():
    config = agent_spawn.load_config(
        {
            "NOTIFICATIONS_SPAWN_ENABLED": "1",
            "NOTIFICATIONS_SPAWN_ROOTS": "/work",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        }
    )
    assert not config.enabled
    assert "managed container" in (config.disabled_reason or "")


def test_config_enabled_requires_roots():
    config = agent_spawn.load_config({"NOTIFICATIONS_SPAWN_ENABLED": "1"})
    assert not config.enabled
    assert "NOTIFICATIONS_SPAWN_ROOTS" in (config.disabled_reason or "")


def test_config_enabled_with_roots_and_knobs():
    config = agent_spawn.load_config(
        {
            "NOTIFICATIONS_SPAWN_ENABLED": "1",
            "NOTIFICATIONS_SPAWN_ROOTS": "/work:/scratch",
            "NOTIFICATIONS_SPAWN_MAX_ACTIVE": "2",
            "NOTIFICATIONS_SPAWN_COOLDOWN_SECONDS": "5",
            "NOTIFICATIONS_SPAWN_CLAUDE_BIN": "/opt/claude",
        }
    )
    assert config.enabled
    assert config.disabled_reason is None
    assert [str(r) for r in config.roots] == ["/work", "/scratch"]
    assert config.max_active == 2
    assert config.cooldown_seconds == 5.0
    assert config.claude_bin == "/opt/claude"


def test_config_prewarm_default_and_override():
    default = agent_spawn.load_config({})
    assert any(p.name == "notifications-server.py" for p in default.prewarm_scripts)
    disabled = agent_spawn.load_config({"NOTIFICATIONS_SPAWN_PREWARM": ""})
    assert disabled.prewarm_scripts == ()


# --- validate_working_dir --------------------------------------------------


def _config(roots):
    return agent_spawn.load_config(
        {
            "NOTIFICATIONS_SPAWN_ENABLED": "1",
            "NOTIFICATIONS_SPAWN_ROOTS": ":".join(str(r) for r in roots),
        }
    )


def test_working_dir_accepts_root_and_nested(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    config = _config([tmp_path])
    assert agent_spawn.validate_working_dir(config, str(tmp_path)) == tmp_path.resolve()
    assert agent_spawn.validate_working_dir(config, str(nested)) == nested.resolve()


def test_working_dir_rejects_missing_relative_and_outside(tmp_path):
    config = _config([tmp_path / "root"])
    (tmp_path / "root").mkdir()
    (tmp_path / "outside").mkdir()
    with pytest.raises(agent_spawn.SpawnError, match="required"):
        agent_spawn.validate_working_dir(config, "")
    with pytest.raises(agent_spawn.SpawnError, match="absolute"):
        agent_spawn.validate_working_dir(config, "relative/path")
    with pytest.raises(agent_spawn.SpawnError, match="does not exist"):
        agent_spawn.validate_working_dir(config, str(tmp_path / "root" / "missing"))
    with pytest.raises(agent_spawn.SpawnError, match="not under"):
        agent_spawn.validate_working_dir(config, str(tmp_path / "outside"))


def test_working_dir_dotdot_cannot_escape(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "secret").mkdir()
    config = _config([tmp_path / "root"])
    sneaky = str(tmp_path / "root" / ".." / "secret")
    with pytest.raises(agent_spawn.SpawnError, match="not under"):
        agent_spawn.validate_working_dir(config, sneaky)


def test_working_dir_symlink_cannot_escape(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "secret").mkdir()
    link = tmp_path / "root" / "link"
    link.symlink_to(tmp_path / "secret")
    config = _config([tmp_path / "root"])
    with pytest.raises(agent_spawn.SpawnError, match="not under"):
        agent_spawn.validate_working_dir(config, str(link))


# --- output parsing --------------------------------------------------------


def test_parse_backgrounded_short_variants():
    assert agent_spawn.parse_backgrounded_short("backgrounded · ab12cd34") == "ab12cd34"
    # ANSI-colored short (chalk cyan), separator present
    colored = "backgrounded \xb7 \x1b[36mab12cd34\x1b[39m · extra"
    assert agent_spawn.parse_backgrounded_short(colored) == "ab12cd34"
    # separator lost in re-encoding
    assert agent_spawn.parse_backgrounded_short("backgrounded ab12cd34\n") == "ab12cd34"
    assert agent_spawn.parse_backgrounded_short("no dispatch banner") is None


def test_parse_agents_json():
    text = json.dumps(
        [
            {"id": "aaaa1111", "status": "idle"},
            {"id": "bbbb2222", "status": "busy"},
            {"noid": True},
        ]
    )
    assert agent_spawn.parse_agents_json(text) == {"aaaa1111", "bbbb2222"}
    with pytest.raises(agent_spawn.SpawnError, match="could not parse"):
        agent_spawn.parse_agents_json("not json")


# --- gates + state ---------------------------------------------------------


def _state(tmp_path):
    return agent_spawn.SpawnState(tmp_path / "spawned.json")


def test_gates_cooldown(tmp_path):
    config = _config([tmp_path])
    state = _state(tmp_path)
    agent_spawn.check_gates(config, state, set(), now=100.0)  # never spawned: fine
    state.touch(100.0)
    with pytest.raises(agent_spawn.SpawnError, match="cooldown"):
        agent_spawn.check_gates(config, state, set(), now=100.0 + 1)
    agent_spawn.check_gates(
        config, state, set(), now=100.0 + config.cooldown_seconds + 1
    )


def test_gates_cap_counts_only_live_recorded_shorts(tmp_path):
    config = agent_spawn.load_config(
        {
            "NOTIFICATIONS_SPAWN_ENABLED": "1",
            "NOTIFICATIONS_SPAWN_ROOTS": str(tmp_path),
            "NOTIFICATIONS_SPAWN_MAX_ACTIVE": "1",
        }
    )
    state = _state(tmp_path)
    state.record({"short": "dead0001"}, now=1.0)
    state.record({"short": "live0001"}, now=2.0)
    # dead0001 is history (not in the live set) so only live0001 counts.
    live = {"live0001", "someone-elses"}
    with pytest.raises(agent_spawn.SpawnError, match="cap"):
        agent_spawn.check_gates(config, state, live, now=1000.0)
    # With its one live spawn gone, the cap clears.
    agent_spawn.check_gates(config, state, {"someone-elses"}, now=1000.0)


def test_state_persists_across_instances(tmp_path):
    state = _state(tmp_path)
    state.record({"short": "ab12cd34", "name": "helper"}, now=42.0)
    reloaded = _state(tmp_path)
    assert reloaded.last_spawn_at == 42.0
    assert reloaded.shorts() == {"ab12cd34"}


def test_state_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "spawned.json"
    path.write_text("{corrupt")
    state = agent_spawn.SpawnState(path)
    assert state.last_spawn_at == 0.0
    assert state.shorts() == set()


# --- argv + env templates --------------------------------------------------


def test_build_argv_message_is_data_after_end_of_options():
    config = agent_spawn.load_config(
        {
            "NOTIFICATIONS_SPAWN_ENABLED": "1",
            "NOTIFICATIONS_SPAWN_ROOTS": "/work",
        }
    )
    argv = agent_spawn.build_argv(config, "--dangerously-evil message", "helper")
    assert argv[0] == "claude"
    assert argv[1] == "--bg"
    # the message rides after `--`, so it can never be parsed as a flag
    assert argv[-2:] == ["--", "--dangerously-evil message"]
    assert argv[argv.index("--name") + 1] == "helper"
    no_name = agent_spawn.build_argv(config, "hi", None)
    assert "--name" not in no_name


def test_build_child_env():
    env = agent_spawn.build_child_env(
        {"PATH": "/bin", "NOTIFICATIONS_RECLAIM_KEY": "shared-key"},
        ws_url="ws://127.0.0.1:9999",
        token="tok",
        name="helper",
    )
    assert env["NOTIFICATIONS_WS_URL"] == "ws://127.0.0.1:9999"
    assert env["NOTIFICATIONS_TOKEN"] == "tok"
    assert env["CLAUDE_CODE_DAEMON_COLD_START"] == "transient"
    assert env["NOTIFICATIONS_RECLAIM_KEY"] == "helper"  # per-child, never inherited
    nameless = agent_spawn.build_child_env(
        {"NOTIFICATIONS_RECLAIM_KEY": "shared-key"},
        ws_url="ws://x",
        token="t",
        name=None,
    )
    assert "NOTIFICATIONS_RECLAIM_KEY" not in nameless
