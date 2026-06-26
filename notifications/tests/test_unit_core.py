# vim: filetype=python
"""Unit tests for the session-id state file, the scheduled-callback store, and the
relay's reconnect backoff math."""

import importlib.util
import os
import time
from pathlib import Path

import anyio
import pytest

import channel_detect as cd
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
