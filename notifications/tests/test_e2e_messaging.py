# vim: filetype=python
"""End-to-end test for Phase-B agent messaging: real daemon + two channel (push)
relay sessions. Proves the wake-gating split both ways — an @here clears B's
default 'direct' threshold and surfaces as a channel event, while a plain fyi
falls below it and is held silently until B's catch_up drains it."""

import anyio
import pytest

import _harness as h

pytestmark = pytest.mark.slow


def test_post_surfaces_or_holds_by_threshold(tmp_path):
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with h.daemon_process(h.daemon_env(ws, store)):

        async def scenario():
            async with (
                h.agent_session(tmp_path, ws, store, xdg, "sid-A") as (read_a, write_a),
                h.agent_session(tmp_path, ws, store, xdg, "sid-B") as (read_b, write_b),
            ):
                # Both register and join the same channel. B keeps the default 'direct'
                # wake threshold.
                text, _ = await h.mcp_call(
                    read_a, write_a, 2, "register_agent", {"name": "agent-a"}
                )
                assert "Registered as 'agent-a'" in text
                text, _ = await h.mcp_call(
                    read_b, write_b, 2, "register_agent", {"name": "agent-b"}
                )
                assert "Registered as 'agent-b'" in text

                text, _ = await h.mcp_call(
                    read_a, write_a, 3, "join_channel", {"channel": "room"}
                )
                assert "Joined #room" in text
                text, _ = await h.mcp_call(
                    read_b, write_b, 3, "join_channel", {"channel": "room"}
                )
                assert "Joined #room" in text

                # An @here (severity=high) is urgent for everyone, so it clears B's
                # 'direct' threshold and surfaces as a channel event.
                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    4,
                    "post",
                    {"channel": "room", "body": "all hands", "severity": "high"},
                )
                assert "Posted to #room" in text
                event = await h.mcp_await_channel_with(read_b, "all hands", timeout=20)
                assert event is not None
                assert "agent-a" in event.params["content"]

                # A plain fyi is ambient — below B's 'direct' threshold — so it is held
                # silently: no channel event arrives within a short bound.
                text, _ = await h.mcp_call(
                    read_a, write_a, 5, "post", {"channel": "room", "body": "fyi thing"}
                )
                assert "Posted to #room" in text
                held_event = await h.mcp_await_channel_with(
                    read_b, "fyi thing", timeout=5
                )
                assert held_event is None  # held, never pushed

                # ...but catch_up drains the held message for B.
                text, _ = await h.mcp_call(read_b, write_b, 6, "catch_up")
                assert "fyi thing" in text

                # And once drained it's acked, so a second catch_up is empty.
                text, _ = await h.mcp_call(read_b, write_b, 7, "catch_up")
                assert "No pending notifications" in text

        anyio.run(scenario)
