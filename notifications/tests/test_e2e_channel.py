# vim: filetype=python
"""End-to-end test for the non-channel (pull) fallback: when the relay detects it
was NOT loaded as a channel, it must buffer notifications instead of pushing them,
expose a `catch_up` tool, and only ack on catch_up."""

import anyio
import pytest

import _harness as h

pytestmark = pytest.mark.slow


def test_pull_mode_buffers_then_catch_up(tmp_path):
    store, xdg, cache = tmp_path / "store", tmp_path / "xdg", tmp_path / "cache"
    store.mkdir()
    xdg.mkdir()
    h.seed_channel_log(cache, registered=False)  # "skipped" -> relay uses pull mode
    ws = h.free_port()
    env = h.relay_env(
        ws, store, xdg, "sid-A", cache_dir=cache, project_dir="/test/proj"
    )

    with h.daemon_process(h.daemon_env(ws, store)):

        async def scenario():
            async with h.stdio_client(h.relay_params(env)) as (read, write):
                await h.mcp_handshake(read, write)

                # Schedule a callback. In pull mode the relay must NOT push it.
                text, channels = await h.mcp_call(
                    read, write, 2, "schedule_test_notification", {"delay_seconds": 2}
                )
                assert "Scheduled callback" in text
                assert not channels
                assert (
                    await h.mcp_await_channel(read, timeout=8) is None
                )  # nothing pushed to the channel

                # catch_up was registered dynamically (tools/list_changed).
                assert "catch_up" in await h.mcp_list_tools(read, write, 3)

                # get_session_id reports the inactive channel + the fallback.
                text, _ = await h.mcp_call(read, write, 4, "get_session_id")
                assert "catch_up to pull" in text

                # catch_up returns the buffered notification, then acks it.
                text, _ = await h.mcp_call(read, write, 5, "catch_up")
                assert "Pending notifications" in text and "sid-A" in text

                # second catch_up is empty (it was acked / drained).
                text, _ = await h.mcp_call(read, write, 6, "catch_up")
                assert "No pending notifications" in text

        anyio.run(scenario)
