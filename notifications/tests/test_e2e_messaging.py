# vim: filetype=python
"""End-to-end test for Phase-B agent messaging: real daemon + two channel (push)
relay sessions. Proves the wake-gating split both ways — an @here clears B's
default 'direct' threshold and surfaces as a channel event, while a plain fyi
falls below it and is held silently until B's catch_up drains it."""

import time
from collections.abc import Callable
from itertools import count

import anyio
import pytest

import _harness as h

pytestmark = pytest.mark.slow


async def _register(read, write, ids, name: str) -> None:
    text, _ = await h.mcp_call(read, write, next(ids), "register_agent", {"name": name})
    assert f"Registered as '{name}'" in text


async def _join(read, write, ids, channel: str, **extra) -> str:
    text, _ = await h.mcp_call(
        read, write, next(ids), "join_channel", {"channel": channel, **extra}
    )
    assert f"Joined #{channel}" in text
    return text


async def _poll_call_until(
    read,
    write,
    ids,
    tool: str,
    predicate: Callable[[str], bool],
    *,
    arguments: dict | None = None,
    timeout: float = 20.0,
    interval: float = 0.2,
) -> str:
    """Poll a string-returning tool until ``predicate`` holds (or timeout). Uses the
    timeout-tolerant call so a single slow relay round-trip is just another retry."""
    deadline = time.monotonic() + timeout
    text = ""
    while True:
        result, _ = await h.mcp_try_call(read, write, next(ids), tool, arguments or {})
        if result is not None:
            text = result
            if predicate(text):
                return text
        if time.monotonic() >= deadline:
            return text
        await anyio.sleep(interval)


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


def test_dm_delivers_and_reuses_thread(tmp_path):
    """A DM is 'direct' for its recipient, so it clears B's default 'direct' bar and
    surfaces as a `[dm] sender: body (→ you)` channel event. A second DM among the same
    participant set, initiated by a different member, reuses the one thread keyed by that
    set: agent-c (only ever in the 3-party DM) ends up in exactly one DM, and agent-a
    (in the earlier 2-party DM plus the reused 3-party one) in exactly two."""
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with h.daemon_process(h.daemon_env(ws, store)):

        async def scenario():
            async with (
                h.agent_session(tmp_path, ws, store, xdg, "sid-A") as (read_a, write_a),
                h.agent_session(tmp_path, ws, store, xdg, "sid-B") as (read_b, write_b),
                h.agent_session(tmp_path, ws, store, xdg, "sid-C") as (read_c, write_c),
            ):
                ids_a, ids_b, ids_c = count(2), count(2), count(2)
                await _register(read_a, write_a, ids_a, "agent-a")
                await _register(read_b, write_b, ids_b, "agent-b")
                await _register(read_c, write_c, ids_c, "agent-c")

                # Single-recipient DM A -> B surfaces as a direct, addressed message.
                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "dm",
                    {"to": ["agent-b"], "body": "ping one"},
                )
                assert "Sent DM to agent-b" in text
                event = await h.mcp_await_channel_with(read_b, "ping one", timeout=20)
                assert event is not None
                content = event.params["content"]
                assert "[dm] agent-a:" in content
                assert "(→ you)" in content  # addressed marker

                # Multi-recipient DM A -> {B, C} opens the 3-party thread.
                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "dm",
                    {"to": ["agent-b", "agent-c"], "body": "hello all"},
                )
                assert "Sent DM to" in text
                assert await h.mcp_await_channel_with(read_b, "hello all", timeout=20)
                assert await h.mcp_await_channel_with(read_c, "hello all", timeout=20)

                # A DM initiated by B to {A, C} lands in that SAME thread (same set).
                text, _ = await h.mcp_call(
                    read_b,
                    write_b,
                    next(ids_b),
                    "dm",
                    {"to": ["agent-a", "agent-c"], "body": "reply all"},
                )
                assert "Sent DM to" in text
                assert await h.mcp_await_channel_with(read_a, "reply all", timeout=20)
                assert await h.mcp_await_channel_with(read_c, "reply all", timeout=20)

                # Thread reuse, proven through subscriptions: agent-c is in exactly one
                # DM (it only ever participated in the 3-party set, and both 3-party
                # messages shared it), while agent-a is in two (the 2-party plus the
                # reused 3-party).
                subs_c, _ = await h.mcp_call(
                    read_c, write_c, next(ids_c), "list_subscriptions"
                )
                assert subs_c.count("(dm)") == 1
                subs_a, _ = await h.mcp_call(
                    read_a, write_a, next(ids_a), "list_subscriptions"
                )
                assert subs_a.count("(dm)") == 2

        anyio.run(scenario)


def test_mention_surfaces_only_for_mentioned(tmp_path):
    """A plain fyi that @-mentions agent-b is 'direct' for B (addressed) but 'ambient'
    for C. B surfaces it with the `(→ you)` marker; C does not surface it within a short
    bound, yet C's catch_up still drains it (ambient, held below C's 'direct' bar)."""
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with h.daemon_process(h.daemon_env(ws, store)):

        async def scenario():
            async with (
                h.agent_session(tmp_path, ws, store, xdg, "sid-A") as (read_a, write_a),
                h.agent_session(tmp_path, ws, store, xdg, "sid-B") as (read_b, write_b),
                h.agent_session(tmp_path, ws, store, xdg, "sid-C") as (read_c, write_c),
            ):
                ids_a, ids_b, ids_c = count(2), count(2), count(2)
                await _register(read_a, write_a, ids_a, "agent-a")
                await _register(read_b, write_b, ids_b, "agent-b")
                await _register(read_c, write_c, ids_c, "agent-c")
                await _join(read_a, write_a, ids_a, "room")
                await _join(read_b, write_b, ids_b, "room")
                await _join(read_c, write_c, ids_c, "room")

                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "post",
                    {"channel": "room", "body": "look here", "mentions": ["agent-b"]},
                )
                assert "Posted to #room" in text

                # B is mentioned -> addressed -> direct, clears B's default bar.
                event = await h.mcp_await_channel_with(read_b, "look here", timeout=20)
                assert event is not None
                assert "(→ you)" in event.params["content"]

                # C is not addressed -> ambient -> held below C's 'direct' bar.
                assert (
                    await h.mcp_await_channel_with(read_c, "look here", timeout=5)
                    is None
                )

                # ...but it is waiting for C in catch_up.
                drained = await _poll_call_until(
                    read_c, write_c, ids_c, "catch_up", lambda t: "look here" in t
                )
                assert "look here" in drained

        anyio.run(scenario)


def test_flush_on_clear_batches_held_backlog(tmp_path):
    """Two plain fyis held below B's 'direct' bar are flushed alongside the @here that
    clears it: B surfaces ONE coalesced channel event carrying both quiet bodies and the
    @here body together."""
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
                ids_a, ids_b = count(2), count(2)
                await _register(read_a, write_a, ids_a, "agent-a")
                await _register(read_b, write_b, ids_b, "agent-b")
                await _join(read_a, write_a, ids_a, "room")
                await _join(read_b, write_b, ids_b, "room")

                for body in ("quiet one", "quiet two"):
                    text, _ = await h.mcp_call(
                        read_a,
                        write_a,
                        next(ids_a),
                        "post",
                        {"channel": "room", "body": body},
                    )
                    assert "Posted to #room" in text
                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "post",
                    {"channel": "room", "body": "all hands now", "severity": "high"},
                )
                assert "Posted to #room" in text

                # The @here clears B's bar; the held backlog flushes in the same event.
                event = await h.mcp_await_channel_with(
                    read_b, "all hands now", timeout=20
                )
                assert event is not None
                content = event.params["content"]
                assert "quiet one" in content
                assert "quiet two" in content
                assert "all hands now" in content

        anyio.run(scenario)


def test_history_tail_rendered_on_join(tmp_path):
    """Messages A posts before B joins #room are not delivered to B as wakes, but the
    join reply renders them as a recent-history scrollback tail."""
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
                ids_a, ids_b = count(2), count(2)
                await _register(read_a, write_a, ids_a, "agent-a")
                await _register(read_b, write_b, ids_b, "agent-b")
                await _join(read_a, write_a, ids_a, "room")

                for body in ("earlier one", "earlier two"):
                    await h.mcp_call(
                        read_a,
                        write_a,
                        next(ids_a),
                        "post",
                        {"channel": "room", "body": body},
                    )

                # B's join reply carries the recent history as scrollback, not a wake.
                join_text = await _join(read_b, write_b, ids_b, "room")
                assert "Recent messages:" in join_text
                assert "earlier one" in join_text
                assert "earlier two" in join_text

                # History is context, not a notification: no channel event fires for it.
                assert (
                    await h.mcp_await_channel_with(read_b, "earlier one", timeout=5)
                    is None
                )

        anyio.run(scenario)


def test_channel_topic_shown_in_list(tmp_path):
    """A topic set on first join, then changed via set_channel_topic, is reflected in
    list_channels both times."""
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
                ids_a = count(2)
                await _register(read_a, write_a, ids_a, "agent-a")
                await _join(read_a, write_a, ids_a, "room", topic="release planning")

                text, _ = await h.mcp_call(
                    read_a, write_a, next(ids_a), "list_channels"
                )
                assert "#room" in text
                assert "release planning" in text

                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "set_channel_topic",
                    {"channel": "room", "topic": "ship it friday"},
                )
                assert "ship it friday" in text

                text, _ = await h.mcp_call(
                    read_a, write_a, next(ids_a), "list_channels"
                )
                assert "ship it friday" in text
                assert "release planning" not in text

        anyio.run(scenario)


def test_set_threshold_changes_surfacing(tmp_path):
    """A per-channel threshold is consulted live at delivery time. With B raised to
    'urgent' on chan:room, a mention (normally 'direct') is held; lowered to 'all', a
    plain fyi (normally 'ambient') surfaces."""
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
                ids_a, ids_b = count(2), count(2)
                await _register(read_a, write_a, ids_a, "agent-a")
                await _register(read_b, write_b, ids_b, "agent-b")
                await _join(read_a, write_a, ids_a, "room")
                await _join(read_b, write_b, ids_b, "room")

                # The context key set_threshold expects is the topic key list_subscriptions
                # reports for the channel: "chan:room".
                subs, _ = await h.mcp_call(
                    read_b, write_b, next(ids_b), "list_subscriptions"
                )
                assert "chan:room" in subs
                text, _ = await h.mcp_call(
                    read_b,
                    write_b,
                    next(ids_b),
                    "set_threshold",
                    {"context": "chan:room", "threshold": "urgent"},
                )
                assert "set to 'urgent'" in text

                # A mention is 'direct' < 'urgent', so it is held (not surfaced).
                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "post",
                    {"channel": "room", "body": "hold me", "mentions": ["agent-b"]},
                )
                assert "Posted to #room" in text
                assert (
                    await h.mcp_await_channel_with(read_b, "hold me", timeout=5) is None
                )

                # Lower the bar to 'all': now even a plain fyi ('ambient') surfaces.
                text, _ = await h.mcp_call(
                    read_b,
                    write_b,
                    next(ids_b),
                    "set_threshold",
                    {"context": "chan:room", "threshold": "all"},
                )
                assert "set to 'all'" in text
                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "post",
                    {"channel": "room", "body": "surface me"},
                )
                assert "Posted to #room" in text
                assert await h.mcp_await_channel_with(read_b, "surface me", timeout=20)

        anyio.run(scenario)


def test_memberless_channel_reaped(tmp_path):
    """Under a low channel TTL, a channel that is left memberless and goes silent past
    the TTL is reaped: it drops out of list_channels."""
    store, xdg = tmp_path / "store", tmp_path / "xdg"
    store.mkdir()
    xdg.mkdir()
    ws = h.free_port()

    with h.daemon_process(h.daemon_env(ws, store, channel_ttl="1")):

        async def scenario():
            async with h.agent_session(tmp_path, ws, store, xdg, "sid-A") as (
                read_a,
                write_a,
            ):
                ids_a = count(2)
                await _register(read_a, write_a, ids_a, "agent-a")
                await _join(read_a, write_a, ids_a, "room")
                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "post",
                    {"channel": "room", "body": "bye"},
                )
                assert "Posted to #room" in text

                text, _ = await h.mcp_call(
                    read_a, write_a, next(ids_a), "list_channels"
                )
                assert "#room" in text

                # Leaving makes #room memberless; once silent past the 1s TTL the reaper
                # deletes it. Poll list_channels until it disappears.
                text, _ = await h.mcp_call(
                    read_a, write_a, next(ids_a), "leave_channel", {"channel": "room"}
                )
                assert "Left #room" in text
                text = await _poll_call_until(
                    read_a, write_a, ids_a, "list_channels", lambda t: "#room" not in t
                )
                assert "#room" not in text

        anyio.run(scenario)


def test_reaction_is_ambient_and_visible_in_status(tmp_path):
    """A reaction is terminal + 'ambient': it never wakes the author (no channel event),
    yet it is recorded — A sees agent-b's 👍 via message_status and, rendered against the
    original message, in catch_up. The first post to a fresh #room is deterministically
    msg:chan:room:0 (seq starts at 0; joining adds no message)."""
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
                ids_a, ids_b = count(2), count(2)
                await _register(read_a, write_a, ids_a, "agent-a")
                await _register(read_b, write_b, ids_b, "agent-b")
                await _join(read_a, write_a, ids_a, "room")
                await _join(read_b, write_b, ids_b, "room")

                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "post",
                    {"channel": "room", "body": "hello"},
                )
                assert "Posted to #room" in text
                hello_id = "msg:chan:room:0"

                text, _ = await h.mcp_call(
                    read_b,
                    write_b,
                    next(ids_b),
                    "react",
                    {"message_id": hello_id, "reaction": "👍"},
                )
                assert "👍" in text and hello_id in text

                # Ambient: the reaction never wakes A (no channel event within a bound).
                assert (
                    await h.mcp_await_channel_with(read_a, "reacted", timeout=5) is None
                )

                # Recorded, though: A sees it in message_status (B still 'pending' on the
                # held fyi; reacting does not ack the reacted-to message).
                status, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "message_status",
                    {"message_id": hello_id},
                )
                assert "👍" in status and "agent-b" in status

                # ...and it drains via catch_up, rendered against the original message.
                drained = await _poll_call_until(
                    read_a, write_a, ids_a, "catch_up", lambda t: "reacted" in t
                )
                assert "👍" in drained
                assert "agent-b reacted" in drained
                assert 'agent-a\'s "hello"' in drained

        anyio.run(scenario)


def test_message_status_reflects_gating(tmp_path):
    """A read receipt tracks the ack-on-surface invariant: a plain fyi held below B's
    'direct' bar reads as pending until B drains it, then flips to delivered. The author
    (agent-a) is excluded from the tally — it pre-acks its own post."""
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
                ids_a, ids_b = count(2), count(2)
                await _register(read_a, write_a, ids_a, "agent-a")
                await _register(read_b, write_b, ids_b, "agent-b")
                await _join(read_a, write_a, ids_a, "room")
                await _join(read_b, write_b, ids_b, "room")

                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "post",
                    {"channel": "room", "body": "fyi thing"},
                )
                assert "Posted to #room" in text
                msg_id = "msg:chan:room:0"

                # Held below B's bar -> B reads as pending; the author is not counted.
                status, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "message_status",
                    {"message_id": msg_id},
                )
                assert "pending: agent-b" in status
                assert "agent-a" not in status  # author excluded from the tally

                # B drains (and acks) the held message.
                drained = await _poll_call_until(
                    read_b, write_b, ids_b, "catch_up", lambda t: "fyi thing" in t
                )
                assert "fyi thing" in drained

                # The receipt now reads delivered for agent-b.
                status = await _poll_call_until(
                    read_a,
                    write_a,
                    ids_a,
                    "message_status",
                    lambda t: "Delivered to 1 of 1" in t,
                    arguments={"message_id": msg_id},
                )
                assert "Delivered to 1 of 1" in status
                assert "agent-b" in status

        anyio.run(scenario)


def test_invalid_reaction_rejected(tmp_path):
    """An empty or newline-containing reaction is rejected loudly client-side and never
    reaches the log: message_status shows no reactions afterward."""
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
                ids_a, ids_b = count(2), count(2)
                await _register(read_a, write_a, ids_a, "agent-a")
                await _register(read_b, write_b, ids_b, "agent-b")
                await _join(read_a, write_a, ids_a, "room")
                await _join(read_b, write_b, ids_b, "room")

                text, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "post",
                    {"channel": "room", "body": "react to me"},
                )
                assert "Posted to #room" in text
                msg_id = "msg:chan:room:0"

                for bad in ("", "bad\nreaction"):
                    text, _ = await h.mcp_call(
                        read_b,
                        write_b,
                        next(ids_b),
                        "react",
                        {"message_id": msg_id, "reaction": bad},
                    )
                    assert "Could not react" in text

                # Nothing was posted: the message carries no reactions.
                status, _ = await h.mcp_call(
                    read_a,
                    write_a,
                    next(ids_a),
                    "message_status",
                    {"message_id": msg_id},
                )
                assert "reactions:" not in status

        anyio.run(scenario)
