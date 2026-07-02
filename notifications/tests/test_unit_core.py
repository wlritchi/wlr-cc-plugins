# vim: filetype=python
"""Unit tests for the session-id state file, the scheduled-callback store, and the
relay's reconnect backoff math."""

import importlib.util
import os
import time
from pathlib import Path

import anyio
import pytest

import _harness as h
import channel_detect as cd
import pr_monitor
import scheduler
import session_state
import wsproto

DEAD_PID = 2147483647  # never a live pid


# --------------------------------------------------------------------------- #
# session_state
# --------------------------------------------------------------------------- #


def test_effective_session_id_prefers_state_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv(session_state.SESSION_ID_ENV_VAR, "env-sid")
    monkeypatch.setattr(session_state, "resolve_claude_pid", lambda: 4242)
    session_state.write_session(4242, "real-sid", "resume")
    sid, source = session_state.effective_session_id()
    assert sid == "real-sid"
    assert "state file" in source


def test_effective_session_id_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv(session_state.SESSION_ID_ENV_VAR, "env-sid")
    monkeypatch.setattr(session_state, "resolve_claude_pid", lambda: None)
    sid, source = session_state.effective_session_id()
    assert sid == "env-sid"
    assert source.startswith("env")


def test_write_read_roundtrip_and_reap(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session_state.write_session(DEAD_PID, "ghost")
    assert session_state.read_session(DEAD_PID) == "ghost"
    session_state.reap_stale()  # DEAD_PID is not alive -> file removed
    assert session_state.read_session(DEAD_PID) is None


def test_read_session_accepts_matching_start_time(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(session_state, "_proc_start_time", lambda pid: 111)
    session_state.write_session(4242, "sid")  # records start_time=111
    assert session_state.read_session(4242) == "sid"  # still 111 -> match


def test_read_session_rejects_recycled_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(session_state, "_proc_start_time", lambda pid: 111)
    session_state.write_session(4242, "sid")  # records start_time=111
    # A new process now holds pid 4242 with a different start time.
    monkeypatch.setattr(session_state, "_proc_start_time", lambda pid: 222)
    assert session_state.read_session(4242) is None  # stale -> ignored


def test_read_session_backward_compat_no_start_time(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    # Older-format file without a start_time key is still accepted.
    path = session_state._state_path(4242)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"session_id": "old-sid", "claude_pid": 4242}')
    monkeypatch.setattr(session_state, "_proc_start_time", lambda pid: 999)
    assert session_state.read_session(4242) == "old-sid"


def test_effective_session_id_env_fallback_on_recycled_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv(session_state.SESSION_ID_ENV_VAR, "env-sid")
    monkeypatch.setattr(session_state, "resolve_claude_pid", lambda: 4242)
    monkeypatch.setattr(session_state, "_proc_start_time", lambda pid: 111)
    session_state.write_session(4242, "real-sid", "resume")  # records 111
    # Pid recycled: the process now holding 4242 has a different start time.
    monkeypatch.setattr(session_state, "_proc_start_time", lambda pid: 222)
    sid, source = session_state.effective_session_id()
    assert sid == "env-sid"
    assert source.startswith("env")


# --------------------------------------------------------------------------- #
# scheduler
# --------------------------------------------------------------------------- #


def test_scheduler_due_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
    callback_id = scheduler.schedule("sid", time.time() - 1, kind="demo")
    due = scheduler.due_callbacks("sid", time.time())
    assert [e["id"] for e in due] == [callback_id]
    assert [e["id"] for e in scheduler.pending("sid")] == [callback_id]
    scheduler.delete("sid", callback_id)
    assert scheduler.pending("sid") == []


def test_scheduler_not_yet_due(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
    scheduler.schedule("sid", time.time() + 1000)
    assert scheduler.due_callbacks("sid", time.time()) == []
    assert len(scheduler.pending("sid")) == 1


# --------------------------------------------------------------------------- #
# wsproto shared token
# --------------------------------------------------------------------------- #


def test_token_autocreates_stable_and_0600(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTIFICATIONS_TOKEN", raising=False)
    monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
    first = wsproto.token()
    assert first
    token_file = tmp_path / "token"
    assert token_file.read_text().strip() == first
    assert (token_file.stat().st_mode & 0o777) == 0o600
    assert wsproto.token() == first  # stable on repeated calls


def test_token_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFICATIONS_TOKEN", "explicit-secret")
    assert wsproto.token() == "explicit-secret"
    assert not (tmp_path / "token").exists()  # env path never touches the file


def test_uri_builds_from_host_port_by_default(monkeypatch):
    monkeypatch.delenv("NOTIFICATIONS_WS_URL", raising=False)
    monkeypatch.setenv("NOTIFICATIONS_WS_HOST", "127.0.0.1")
    monkeypatch.setenv("NOTIFICATIONS_WS_PORT", "8137")
    assert wsproto.uri() == "ws://127.0.0.1:8137"


def test_uri_full_url_override_wins(monkeypatch):
    # A remote daemon behind a TLS-terminating ingress: the relay connects via wss://
    # and the host/port are ignored entirely.
    monkeypatch.setenv("NOTIFICATIONS_WS_HOST", "127.0.0.1")
    monkeypatch.setenv("NOTIFICATIONS_WS_PORT", "8137")
    monkeypatch.setenv("NOTIFICATIONS_WS_URL", "wss://notifications.d.example.com")
    assert wsproto.uri() == "wss://notifications.d.example.com"


# --------------------------------------------------------------------------- #
# channel-load detection
# --------------------------------------------------------------------------- #


def _write_log(cache_root, project_dir, server_dir, ts, content):
    directory = (
        cache_root / "claude-cli-nodejs" / cd.encode_cwd(project_dir) / server_dir
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ts}.jsonl"
    path.write_text(content)
    return path


class TestChannelDetect:
    PROJECT = "/home/u/proj"
    SERVER_DIR = "mcp-logs-plugin-notifications-notifications"

    def test_encode_cwd(self):
        assert cd.encode_cwd("/home/u/wlr-cc-plugins") == "-home-u-wlr-cc-plugins"
        assert cd.encode_cwd("/home/u/.claude/x") == "-home-u--claude-x"

    def test_cache_root_override_wins_over_darwin(self, tmp_path, monkeypatch):
        # The explicit override must bypass the platform branch entirely, so even on
        # macOS (simulated) detection points at the given cache root, not ~/Library/Caches.
        monkeypatch.setattr(cd.sys, "platform", "darwin")
        monkeypatch.setenv("NOTIFICATIONS_MCP_LOG_CACHE_DIR", str(tmp_path))
        assert cd._cache_root() == tmp_path

    def test_cache_root_without_override_honors_xdg(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOTIFICATIONS_MCP_LOG_CACHE_DIR", raising=False)
        monkeypatch.setattr(cd.sys, "platform", "linux")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert cd._cache_root() == tmp_path

    def test_cache_root_without_override_falls_back_to_dot_cache(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("NOTIFICATIONS_MCP_LOG_CACHE_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr(cd.sys, "platform", "linux")
        assert cd._cache_root() == Path.home() / ".cache"

    def test_override_makes_detection_work_on_darwin(self, tmp_path, monkeypatch):
        # Full round-trip proving the seam works on macOS: a seeded "registered" log
        # under the override cache root is found even on a simulated darwin platform.
        monkeypatch.setattr(cd.sys, "platform", "darwin")
        monkeypatch.setenv("NOTIFICATIONS_MCP_LOG_CACHE_DIR", str(tmp_path))
        _write_log(
            tmp_path,
            self.PROJECT,
            self.SERVER_DIR,
            "2026-06-26T00-00-00Z",
            '{"message":"Channel notifications registered"}\n',
        )
        assert cd.detect_channel_mode("notifications", self.PROJECT) == cd.REGISTERED

    def test_registered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cd, "_cache_root", lambda: tmp_path)
        _write_log(
            tmp_path,
            self.PROJECT,
            self.SERVER_DIR,
            "2026-06-26T00-00-00Z",
            '{"message":"Channel notifications registered"}\n',
        )
        assert cd.detect_channel_mode("notifications", self.PROJECT) == cd.REGISTERED

    def test_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cd, "_cache_root", lambda: tmp_path)
        _write_log(
            tmp_path,
            self.PROJECT,
            self.SERVER_DIR,
            "2026-06-26T00-00-00Z",
            '{"message":"Channel notifications skipped: not in --channels list"}\n',
        )
        assert cd.detect_channel_mode("notifications", self.PROJECT) == cd.SKIPPED

    def test_unknown_when_no_logs_or_no_marker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cd, "_cache_root", lambda: tmp_path)
        assert (
            cd.detect_channel_mode("notifications", self.PROJECT) == cd.UNKNOWN
        )  # no dir
        _write_log(
            tmp_path,
            self.PROJECT,
            self.SERVER_DIR,
            "2026-06-26T00-00-00Z",
            '{"message":"some other line"}\n',
        )
        assert (
            cd.detect_channel_mode("notifications", self.PROJECT) == cd.UNKNOWN
        )  # no marker
        assert (
            cd.detect_channel_mode("notifications", None) == cd.UNKNOWN
        )  # no project dir

    def test_newer_than_filters_stale_logs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cd, "_cache_root", lambda: tmp_path)
        log = _write_log(
            tmp_path,
            self.PROJECT,
            self.SERVER_DIR,
            "2026-06-26T00-00-00Z",
            '{"message":"Channel notifications registered"}\n',
        )
        old = time.time() - 10_000
        os.utime(log, (old, old))
        assert (
            cd.detect_channel_mode(
                "notifications", self.PROJECT, newer_than=time.time()
            )
            == cd.UNKNOWN
        )
        assert (
            cd.detect_channel_mode("notifications", self.PROJECT, newer_than=0)
            == cd.REGISTERED
        )

    def test_latest_log_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cd, "_cache_root", lambda: tmp_path)
        old = _write_log(
            tmp_path,
            self.PROJECT,
            self.SERVER_DIR,
            "2026-06-26T00-00-00Z",
            '{"message":"Channel notifications skipped: x"}\n',
        )
        os.utime(old, (time.time() - 100, time.time() - 100))
        _write_log(
            tmp_path,
            self.PROJECT,
            self.SERVER_DIR,
            "2026-06-26T01-00-00Z",
            '{"message":"Channel notifications registered"}\n',
        )
        assert cd.detect_channel_mode("notifications", self.PROJECT) == cd.REGISTERED


# --------------------------------------------------------------------------- #
# relay reconnect backoff
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def relay():
    path = Path(__file__).resolve().parent.parent / "mcp" / "notifications-server.py"
    spec = importlib.util.spec_from_file_location("notifications_relay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backoff_grows_then_caps_with_jitter(relay):
    for failures in range(0, 14):
        nominal = min(
            relay.RECONNECT_MAX_SECONDS, relay.RECONNECT_INITIAL_SECONDS * 2**failures
        )
        samples = [relay._reconnect_delay(failures) for _ in range(2000)]
        assert min(samples) >= nominal * (1 - relay.RECONNECT_JITTER) - 1e-6
        assert max(samples) <= nominal * (1 + relay.RECONNECT_JITTER) + 1e-6


def test_backoff_steady_state_is_about_30_min(relay):
    assert relay.RECONNECT_MAX_SECONDS == 30 * 60
    samples = [relay._reconnect_delay(20) for _ in range(2000)]
    assert 24 * 60 <= min(samples)
    assert max(samples) <= 36 * 60


def test_wait_connected_nudges_reconnect_when_disconnected(relay):
    """The foreground path: a disconnected caller must NUDGE the reconnect loop
    (set _reconnect_now) so it breaks its idle-tuned backoff and retries at once,
    rather than passively waiting out the up-to-30-min sleep."""
    client = relay.DaemonClient()
    assert not client.connected  # fresh client, no socket

    async def scenario():
        return await client.wait_connected(timeout=0.2)  # no daemon → False, fast

    assert anyio.run(scenario) is False
    assert client._reconnect_now.is_set()  # the nudge was emitted


def test_detect_prefers_worktree_cwd_log(relay, tmp_path, monkeypatch):
    """A worktree session's MCP log is keyed by the session *cwd* (the worktree
    path), while CLAUDE_PROJECT_DIR still names the main repo root. Detection must
    probe the cwd first and find the real registration there, instead of reading
    the (markerless) main-repo log dir and downgrading to pull."""
    worktree = tmp_path / "proj" / ".claude" / "worktrees" / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "proj"))
    cache = tmp_path / "cache"
    monkeypatch.setenv("NOTIFICATIONS_MCP_LOG_CACHE_DIR", str(cache))
    h.seed_channel_log(cache, registered=True, project_dir=str(worktree))

    client = relay.DaemonClient()
    anyio.run(client.detect_and_apply)
    assert client._mode == "push"


def test_detect_falls_back_to_project_dir_log(relay, tmp_path, monkeypatch):
    """When the cwd has no MCP log at all, the CLAUDE_PROJECT_DIR candidate must
    still be probed (the pre-worktree behavior)."""
    proj = tmp_path / "proj"
    elsewhere = tmp_path / "elsewhere"
    proj.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    cache = tmp_path / "cache"
    monkeypatch.setenv("NOTIFICATIONS_MCP_LOG_CACHE_DIR", str(cache))
    h.seed_channel_log(cache, registered=True, project_dir=str(proj))

    client = relay.DaemonClient()
    anyio.run(client.detect_and_apply)
    assert client._mode == "push"


def test_wait_connected_does_not_nudge_when_already_connected(relay):
    """Already connected → nothing to reconnect, so no nudge is emitted."""
    client = relay.DaemonClient()
    client._ws = object()  # pretend connected

    async def scenario():
        return await client.wait_connected(timeout=0.2)

    assert anyio.run(scenario) is True
    assert not client._reconnect_now.is_set()


# --------------------------------------------------------------------------- #
# relay push-mode debounce / coalescing
# --------------------------------------------------------------------------- #


def _push_client(relay):
    """A push-mode DaemonClient with _deliver_channel/_ack replaced by recorders."""
    client = relay.DaemonClient()
    client._mode = "push"
    delivered: list[tuple[str, dict | None]] = []
    acked: list = []

    async def fake_deliver(content, meta):
        delivered.append((content, meta))

    async def fake_ack(notification_id):
        acked.append(notification_id)

    client._deliver_channel = fake_deliver
    client._ack = fake_ack
    return client, delivered, acked


def test_flush_coalesces_multiple_into_one_event(relay, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DEBOUNCE_SECONDS", "1")
    client, delivered, acked = _push_client(relay)

    async def scenario():
        await client._enqueue_push(
            "id1", "first", {"severity": "info", "kind": "pr_check"}
        )
        await client._enqueue_push(
            "id2", "second", {"severity": "high", "kind": "pr_review"}
        )
        await client._enqueue_push(
            "id3", "third", {"severity": "info", "kind": "pr_comment"}
        )
        assert delivered == []  # buffered, nothing pushed yet
        await client._flush_debounce()

    anyio.run(scenario)

    assert len(delivered) == 1  # one coalesced channel event
    content, meta = delivered[0]
    assert content == "first\n\nsecond\n\nthird"  # blank line between each
    assert meta == {"severity": "high", "kind": "batch", "count": "3"}
    assert acked == ["id1", "id2", "id3"]  # every underlying id acked
    assert client._pending_debounce == []


def test_flush_single_item_keeps_its_kind(relay, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DEBOUNCE_SECONDS", "1")
    client, delivered, acked = _push_client(relay)

    async def scenario():
        await client._enqueue_push(
            "only", "solo", {"severity": "info", "kind": "pr_comment"}
        )
        await client._flush_debounce()

    anyio.run(scenario)
    assert delivered == [
        ("solo", {"severity": "info", "kind": "pr_comment", "count": "1"})
    ]
    assert acked == ["only"]


def test_delivery_hint_is_mode_aware(relay):
    client = relay.DaemonClient()

    client._mode = "push"
    push_hint = client.delivery_hint()
    assert "<channel>" in push_hint and "catch_up" not in push_hint

    client._mode = "pull"
    pull_hint = client.delivery_hint()
    assert "catch_up" in pull_hint  # pull mode must point the agent at catch_up
    assert "<channel>" not in pull_hint  # and must not promise channel events

    client._mode = None  # detection unresolved: cover both outcomes
    detecting_hint = client.delivery_hint()
    assert "<channel>" in detecting_hint and "catch_up" in detecting_hint


def test_debounce_disabled_delivers_immediately(relay, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DEBOUNCE_SECONDS", "0")
    client, delivered, acked = _push_client(relay)

    async def scenario():
        await client._enqueue_push(
            "id1", "now", {"severity": "info", "kind": "pr_check"}
        )
        # delivered immediately with the original meta passed straight through
        assert delivered == [("now", {"severity": "info", "kind": "pr_check"})]
        assert acked == ["id1"]
        assert client._pending_debounce == []

    anyio.run(scenario)


def test_debounce_loop_flushes_quiet_burst(relay, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DEBOUNCE_SECONDS", "0.2")
    client, delivered, acked = _push_client(relay)

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(client.debounce_loop)
            await client._enqueue_push("a", "alpha", {"severity": "info", "kind": "x"})
            await client._enqueue_push("b", "beta", {"severity": "info", "kind": "y"})
            with anyio.move_on_after(3):  # let the quiet window elapse and flush
                while not delivered:
                    await anyio.sleep(0.05)
            tg.cancel_scope.cancel()

    anyio.run(scenario)
    assert len(delivered) == 1
    content, meta = delivered[0]
    assert content == "alpha\n\nbeta"
    assert meta["kind"] == "batch" and meta["count"] == "2"
    assert acked == ["a", "b"]


# --------------------------------------------------------------------------- #
# daemon warm-retention TTL + reaper
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def daemon():
    path = Path(__file__).resolve().parent.parent / "daemon" / "notifications-daemon.py"
    spec = importlib.util.spec_from_file_location("notifications_daemon", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_tracker(
    daemon, key="o/r#1", *, subscribers=(), terminal=False, idle_since=None
):
    t = pr_monitor.PRTracker("o", "r", 1, None)
    t.key = key
    t.subscribers = set(subscribers)
    t.terminal = terminal
    t.idle_since = idle_since
    return t


def test_warm_ttl_seconds_default_and_overrides(daemon, monkeypatch):
    monkeypatch.delenv("NOTIFICATIONS_PR_WARM_TTL_SECONDS", raising=False)
    assert daemon._warm_ttl_seconds() == 1800.0
    monkeypatch.setenv("NOTIFICATIONS_PR_WARM_TTL_SECONDS", "0")
    assert daemon._warm_ttl_seconds() == 0.0
    monkeypatch.setenv("NOTIFICATIONS_PR_WARM_TTL_SECONDS", "5")
    assert daemon._warm_ttl_seconds() == 5.0
    monkeypatch.setenv("NOTIFICATIONS_PR_WARM_TTL_SECONDS", "garbage")
    assert daemon._warm_ttl_seconds() == 1800.0


def test_reaper_removes_expired_tracker(daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFICATIONS_PR_WARM_TTL_SECONDS", "10")
    now = time.time()
    t = _make_tracker(daemon, idle_since=now - 100)
    monkeypatch.setattr(daemon, "TRACKERS", {t.key: t})
    removed = daemon._reap_idle_trackers(now)
    assert removed == [t.key]
    assert t.key not in daemon.TRACKERS


def test_reaper_keeps_recently_idle_tracker(daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFICATIONS_PR_WARM_TTL_SECONDS", "10")
    now = time.time()
    t = _make_tracker(daemon, idle_since=now - 1)
    monkeypatch.setattr(daemon, "TRACKERS", {t.key: t})
    assert daemon._reap_idle_trackers(now) == []
    assert t.key in daemon.TRACKERS


def test_reaper_skips_subscribed_and_terminal(daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFICATIONS_PR_WARM_TTL_SECONDS", "10")
    now = time.time()
    subbed = _make_tracker(
        daemon, key="o/r#1", subscribers=("sid",), idle_since=now - 100
    )
    term = _make_tracker(daemon, key="o/r#2", terminal=True, idle_since=now - 100)
    monkeypatch.setattr(daemon, "TRACKERS", {subbed.key: subbed, term.key: term})
    assert daemon._reap_idle_trackers(now) == []
    assert subbed.key in daemon.TRACKERS
    assert term.key in daemon.TRACKERS


def test_reaper_self_heals_missing_idle_marker(daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFICATIONS_PR_WARM_TTL_SECONDS", "10")
    now = time.time()
    t = _make_tracker(daemon, idle_since=None)
    monkeypatch.setattr(daemon, "TRACKERS", {t.key: t})
    assert daemon._reap_idle_trackers(now) == []  # clock just started, not removed
    assert t.key in daemon.TRACKERS
    assert t.idle_since == pytest.approx(now)


def test_reaper_disabled_keeps_everything(daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFICATIONS_PR_WARM_TTL_SECONDS", "0")
    t = _make_tracker(daemon, idle_since=time.time() - 10_000)
    monkeypatch.setattr(daemon, "TRACKERS", {t.key: t})
    assert daemon._reap_idle_trackers(time.time()) == []
    assert t.key in daemon.TRACKERS
