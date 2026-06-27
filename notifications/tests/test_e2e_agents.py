# vim: filetype=python
"""End-to-end test for the Phase-A agent directory: real daemon + two relay
sessions. Proves the register/list vertical — one session registers as an agent
and a second, independent session sees it (connected, with its profile) through
list_agents. The messaging/threshold-consuming behaviour is later-phase; this
only exercises the identity + presence wiring."""

import time
from collections.abc import Callable

import anyio
import pytest

import _harness as h

pytestmark = pytest.mark.slow


async def _list_until(
    read,
    write,
    start_id: int,
    predicate: Callable[[str], bool],
    *,
    timeout: float = 20.0,
    interval: float = 0.1,
) -> tuple[str, int]:
    """Poll list_agents until `predicate(text)` holds (or `timeout` elapses).

    Presence flips asynchronously once the daemon processes a disconnect, so a
    single fixed sleep can't be trusted to be long enough; this retries on a short
    interval until the directory settles. Uses the timeout-tolerant call so a single
    slow relay round-trip under load is just another retry, not a hard failure.
    Returns the last (non-None) directory text and the next free request id (ids must
    stay unique within one connection)."""
    deadline = time.monotonic() + timeout
    req_id = start_id
    text = ""
    while True:
        result, _ = await h.mcp_try_call(read, write, req_id, "list_agents", {})
        req_id += 1
        if result is not None:
            text = result
            if predicate(text):
                return text, req_id
        if time.monotonic() >= deadline:
            return text, req_id
        await anyio.sleep(interval)


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


def test_two_agent_directory(tmp_path):
    """Two live sessions each register; either one's list_agents shows both, both
    connected, each carrying its own profile."""
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

                async with h.agent_session(tmp_path, ws, store, xdg, "sid-B") as (
                    read_b,
                    write_b,
                ):
                    text, _ = await h.mcp_call(
                        read_b,
                        write_b,
                        2,
                        "register_agent",
                        {"name": "agent-b", "description": "does B things"},
                    )
                    assert "Registered as 'agent-b'" in text

                    text_a, _ = await h.mcp_call(read_a, write_a, 3, "list_agents", {})
                    text_b, _ = await h.mcp_call(read_b, write_b, 3, "list_agents", {})
                    for text in (text_a, text_b):
                        assert "agent-a" in text
                        assert "agent-b" in text
                        assert "does A things" in text
                        assert "does B things" in text
                        # Both relays are attached, so neither shows as offline and
                        # exactly two "connected" presence markers appear.
                        assert "offline" not in text
                        assert text.count("connected") == 2

        anyio.run(scenario)


def test_collision_rejected_across_live_sessions(tmp_path):
    """B (a different live session) cannot steal A's live name: the register fails
    with the 'already taken' reason and B is left unregistered — the directory still
    holds a single agent-a."""
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
                    read_a, write_a, 2, "register_agent", {"name": "agent-a"}
                )
                assert "Registered as 'agent-a'" in text

                async with h.agent_session(tmp_path, ws, store, xdg, "sid-B") as (
                    read_b,
                    write_b,
                ):
                    text, _ = await h.mcp_call(
                        read_b, write_b, 2, "register_agent", {"name": "agent-a"}
                    )
                    assert "Could not register as 'agent-a'" in text
                    assert "already taken" in text

                    # B never took the name: the directory still lists exactly one
                    # agent-a (one presence line, marked by its em-dash separator).
                    text, _ = await h.mcp_call(read_b, write_b, 3, "list_agents", {})
                    assert "agent-a" in text
                    assert text.count(" — ") == 1

                    # The rejection didn't wedge B's session: it can still claim a
                    # free name, and the directory then holds both agents.
                    text, _ = await h.mcp_call(
                        read_b, write_b, 4, "register_agent", {"name": "agent-b"}
                    )
                    assert "Registered as 'agent-b'" in text

                    text, _ = await h.mcp_call(read_b, write_b, 5, "list_agents", {})
                    assert "agent-a" in text
                    assert "agent-b" in text
                    assert text.count(" — ") == 2

        anyio.run(scenario)


def test_set_availability_reflected_in_list(tmp_path):
    """After set_availability('urgent'), the agent's wake threshold shows as urgent
    in list_agents (it registered with the default 'direct')."""
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
                    read_a, write_a, 2, "register_agent", {"name": "agent-a"}
                )
                assert "Registered as 'agent-a'" in text
                assert "direct" in text  # default wake threshold

                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    3,
                    "set_availability",
                    {"default_threshold": "urgent"},
                )
                assert "urgent" in text

                text, _ = await h.mcp_call(read_a, write_a, 4, "list_agents", {})
                assert "wake threshold: urgent" in text

                # A plain re-register (no default_threshold given) updates profile
                # fields but must NOT reset the threshold back to 'direct': the relay
                # omits default_threshold when unset, and the registry leaves it
                # untouched. The register reply itself echoes the preserved 'urgent'.
                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    5,
                    "register_agent",
                    {"name": "agent-a", "description": "updated"},
                )
                assert "Registered as 'agent-a'" in text
                assert "wake threshold: urgent" in text

                text, _ = await h.mcp_call(read_a, write_a, 6, "list_agents", {})
                assert "updated" in text  # profile field did update
                assert "wake threshold: urgent" in text
                assert "wake threshold: direct" not in text

        anyio.run(scenario)


def test_disconnect_marks_offline(tmp_path):
    """Once A's relay session closes, a list_agents from B reports agent-a as offline
    rather than connected. Presence flips asynchronously, so poll with a bounded
    retry instead of a single sleep."""
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
                    read_a, write_a, 2, "register_agent", {"name": "agent-a"}
                )
                assert "Registered as 'agent-a'" in text
            # A's relay context has exited; its connection is torn down.

            async with h.agent_session(tmp_path, ws, store, xdg, "sid-B") as (
                read_b,
                write_b,
            ):
                text, _ = await _list_until(
                    read_b, write_b, 2, lambda t: "offline" in t
                )
                assert "agent-a" in text
                assert "offline" in text

        anyio.run(scenario)


def test_unregister_removes_from_directory(tmp_path):
    """After unregister_agent, the name is gone from the directory; since it was the
    only agent, list_agents reports none registered."""
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
                    read_a, write_a, 2, "register_agent", {"name": "agent-a"}
                )
                assert "Registered as 'agent-a'" in text

                text, _ = await h.mcp_call(read_a, write_a, 3, "unregister_agent", {})
                assert "Unregistered 'agent-a'" in text

                text, _ = await h.mcp_call(read_a, write_a, 4, "list_agents", {})
                assert "agent-a" not in text
                assert "none registered" in text

        anyio.run(scenario)


def test_reclaim_after_grace(tmp_path):
    """With the reclaim grace set to 0, an abandoned name becomes claimable: A
    registers then disconnects, and once A is seen offline B takes over agent-a,
    which then shows connected under B's profile."""
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with h.daemon_process(h.daemon_env(ws, store, agent_ttl="0")):

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
                    {"name": "agent-a", "description": "A original"},
                )
                assert "Registered as 'agent-a'" in text
            # A is gone; its name is now reclaimable once the daemon drops it from
            # the live connection set.

            async with h.agent_session(tmp_path, ws, store, xdg, "sid-B") as (
                read_b,
                write_b,
            ):
                # Gate the reclaim on A actually being offline: while A is still
                # counted live the registry rejects the name regardless of ttl.
                text, next_id = await _list_until(
                    read_b, write_b, 2, lambda t: "offline" in t
                )
                assert "offline" in text

                text, _ = await h.mcp_call(
                    read_b,
                    write_b,
                    next_id,
                    "register_agent",
                    {"name": "agent-a", "description": "B reclaimed"},
                )
                assert "Registered as 'agent-a'" in text

                text, _ = await h.mcp_call(
                    read_b, write_b, next_id + 1, "list_agents", {}
                )
                assert "agent-a" in text
                assert "connected" in text
                assert "offline" not in text
                assert "B reclaimed" in text  # B's profile now owns the name
                assert "A original" not in text

        anyio.run(scenario)
