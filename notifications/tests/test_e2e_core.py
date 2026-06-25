# vim: filetype=python
"""End-to-end tests for the daemon + relay scheduled-callback path and reconnection.

These spawn the real daemon and relay via `uv run` and drive them with a raw MCP
client over stdio. No network: scheduled callbacks need no GitHub."""

import time

import anyio
import pytest

import _harness as h
import scheduler

pytestmark = pytest.mark.slow

EXPECTED_TOOLS = [
    "get_session_id",
    "list_github_pr_subscriptions",
    "list_scheduled_notifications",
    "schedule_test_notification",
    "subscribe_github_pr",
    "unsubscribe_github_pr",
]


def test_schedule_deliver_and_ack(tmp_path):
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()
    with h.daemon_process(h.daemon_env(ws, store)):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.relay_env(ws, store, xdg, "sid-A"))
            ) as (read, write):
                caps = await h.mcp_handshake(read, write)
                assert caps.get("experimental", {}).get("claude/channel") == {}
                assert await h.mcp_list_tools(read, write, 2) == EXPECTED_TOOLS

                text, channels = await h.mcp_call(
                    read, write, 3, "schedule_test_notification", {"delay_seconds": 2}
                )
                assert "Scheduled callback" in text
                event = channels[0] if channels else await h.mcp_await_channel(read)
                assert event is not None and "sid-A" in event.params["content"]

        anyio.run(scenario)
    # delivered + acked -> the daemon removed the callback file
    assert scheduler_pending(store, "sid-A") == 0


def test_recovery_of_past_due_callback(tmp_path, monkeypatch):
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    # pre-seed a callback that came due while nothing was running
    monkeypatch.setenv("NOTIFICATIONS_DATA_DIR", str(store))
    scheduler.schedule("sid-A", time.time() - 300, kind="scheduled_test")

    ws = h.free_port()
    with h.daemon_process(h.daemon_env(ws, store)):

        async def scenario():
            async with h.stdio_client(
                h.relay_params(h.relay_env(ws, store, xdg, "sid-A"))
            ) as (read, write):
                await h.mcp_handshake(read, write)
                event = await h.mcp_await_channel(
                    read, timeout=25
                )  # no tool call; daemon recovers it
                assert event is not None
                assert (
                    "recovered after restart" in event.params["content"]
                    and "sid-A" in event.params["content"]
                )

        anyio.run(scenario)
    assert scheduler_pending(store, "sid-A") == 0


def test_reconnects_when_daemon_appears(tmp_path):
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    async def scenario():
        async with h.stdio_client(
            h.relay_params(h.relay_env(ws, store, xdg, "sid-A"))
        ) as (read, write):
            await h.mcp_handshake(read, write)
            text, _ = await h.mcp_call(read, write, 2, "get_session_id")
            assert "disconnected" in text  # daemon is not up yet

            with h.daemon_process(h.daemon_env(ws, store)):  # now bring it up
                request_id = 3
                connected = False
                deadline = time.time() + 40
                while time.time() < deadline:
                    text, _ = await h.mcp_call(
                        read, write, request_id, "get_session_id"
                    )
                    request_id += 1
                    if "daemon: connected" in text:
                        connected = True
                        break
                    await anyio.sleep(2)
                assert connected, "relay never reconnected after the daemon came up"

                text, channels = await h.mcp_call(
                    read,
                    write,
                    request_id,
                    "schedule_test_notification",
                    {"delay_seconds": 2},
                )
                assert "Scheduled callback" in text
                event = channels[0] if channels else await h.mcp_await_channel(read)
                assert event is not None and "sid-A" in event.params["content"]

    anyio.run(scenario)


def scheduler_pending(store, session_id) -> int:
    session_dir = store / "scheduled" / session_id
    return len(list(session_dir.glob("*.json"))) if session_dir.is_dir() else 0
