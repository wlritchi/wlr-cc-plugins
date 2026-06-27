# vim: filetype=python
"""End-to-end test for the Phase-A agent directory: real daemon + two relay
sessions. Proves the register/list vertical — one session registers as an agent
and a second, independent session sees it (connected, with its profile) through
list_agents. The messaging/threshold-consuming behaviour is later-phase; this
only exercises the identity + presence wiring."""

import anyio
import pytest

import _harness as h

pytestmark = pytest.mark.slow


def test_register_then_listed_as_connected(tmp_path):
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with h.daemon_process(h.daemon_env(ws, store)):

        async def scenario():
            async with h.agent_session(tmp_path, ws, store, xdg, "sid-A") as (
                read_a,
                write_a,
            ):
                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    2,
                    "register_agent",
                    {"name": "agent-a", "description": "does A things"},
                )
                assert "Registered as 'agent-a'" in text
                assert (
                    "direct" in text
                )  # default wake threshold, echoed from the record

                # A second, independent session sees agent-a in the directory, marked
                # connected (sid-A's relay is still attached), with its profile.
                async with h.agent_session(tmp_path, ws, store, xdg, "sid-B") as (
                    read_b,
                    write_b,
                ):
                    text, _ = await h.mcp_call(read_b, write_b, 2, "list_agents", {})
                    assert "agent-a" in text
                    assert "connected" in text
                    assert "does A things" in text

        anyio.run(scenario)
