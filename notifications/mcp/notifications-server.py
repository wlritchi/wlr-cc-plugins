#!/usr/bin/env -S uv run -qs
# vim: filetype=python
"""
Notifications MCP relay

A thin per-session stdio MCP server. It owns no schedule and no persistence; all
of that lives in the persistent notifications daemon (../daemon/notifications-daemon.py).
This relay only knows how to:

  - find its own Claude Code session id and stay current on it (SessionStart hook
    + state file, see ../lib/session_state.py)
  - open a WebSocket to the daemon and register under that session id
  - receive notifications from the daemon and deliver them to the agent
  - acknowledge delivered notifications back to the daemon
  - forward the agent's schedule/subscribe tool calls to the daemon

Delivery is mode-aware. The relay reads Claude Code's per-server MCP log (see
../lib/channel_detect.py) to learn whether it was loaded as a channel:
  - loaded as a channel  -> push events as notifications/claude/channel (auto-ack)
  - not a channel        -> buffer events (no auto-ack) and expose a `catch_up`
                            tool the agent calls to pull and ack them
Until detection resolves, events are buffered (never lost). The session is loaded
as a channel when launched with e.g.
`claude --dangerously-load-development-channels plugin:notifications@wlr-cc-plugins`.

The daemon must be running (it is started manually or via systemd --user; this
relay never spawns it). If it isn't reachable, the relay keeps retrying and the
tools report it as unavailable.

Run directly: ./notifications-server.py
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp", "anyio", "websockets"]
# ///

import json
import os
import random
import re
import sys
import time
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import channel_detect  # noqa: E402
import messaging  # noqa: E402
import session_state  # noqa: E402
import wsproto  # noqa: E402

CHANNEL_METHOD = "notifications/claude/channel"
SERVER_NAME = "notifications"  # used to locate this server's Claude Code MCP log
# How long to wait for Claude Code to log whether we were loaded as a channel.
CHANNEL_DETECT_TIMEOUT_SECONDS = 12.0
CHANNEL_DETECT_POLL_SECONDS = 0.5
SESSION_POLL_SECONDS = 5.0
# Settle time before connecting, so a recovered (past-due) event isn't pushed
# into the channel before the client has finished the MCP/channel handshake.
STARTUP_GRACE_SECONDS = 3.0
REQUEST_TIMEOUT_SECONDS = 10.0

# Debounce/coalesce window for push (channel) delivery. A burst of notifications
# (e.g. several PR check/review/comment events landing in one poll) is coalesced
# into ONE channel event instead of several separate interruptions. The window is
# a quiet period, reset on each new arrival, capped so a continuous stream still
# flushes periodically. NOTIFICATIONS_DEBOUNCE_SECONDS overrides the quiet window
# (a float; <= 0 disables debounce and delivers each notification immediately).
DEBOUNCE_DEFAULT_SECONDS = 2.0


def _debounce_window() -> float:
    raw = os.environ.get("NOTIFICATIONS_DEBOUNCE_SECONDS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEBOUNCE_DEFAULT_SECONDS


def _debounce_max(window: float) -> float:
    """Max hold since the first pending item, so a steady stream still flushes."""
    return max(window * 5, 10.0)


# Reconnect backoff: binary (exponential) growth with +/- jitter, capped at a
# jittered 30 minutes, after which it retries roughly every half hour forever.
# The counter resets only after a connection that stayed up at least
# RECONNECT_STABLE_SECONDS, so a flapping daemon still backs off.
RECONNECT_INITIAL_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30 * 60
RECONNECT_JITTER = 0.2  # +/- 20%
RECONNECT_STABLE_SECONDS = 30.0
# Cap the exponent so 2**failures can't blow up (saturates well past the cap).
_RECONNECT_MAX_FAILURES = 16


def _reconnect_delay(failures: int) -> float:
    """Exponential-with-jitter backoff for `failures` consecutive bad attempts."""
    nominal = min(RECONNECT_MAX_SECONDS, RECONNECT_INITIAL_SECONDS * 2**failures)
    return nominal * random.uniform(1.0 - RECONNECT_JITTER, 1.0 + RECONNECT_JITTER)


INSTRUCTIONS = (
    "This plugin delivers notifications (scheduled callbacks, GitHub PR updates, and "
    "agent messages) through the Claude Code channels feature. Events normally arrive "
    'on their own as <channel source="notifications" ...> messages; when one arrives, '
    "surface it to the user. Quiet agent messages that fall below your wake threshold "
    "are held silently — call `catch_up` periodically (and after long pauses) to drain "
    "them. If this session was NOT loaded as a channel, nothing is pushed automatically "
    "and `catch_up` is the only way to retrieve pending updates."
)

mcp = FastMCP("notifications", instructions=INSTRUCTIONS)


class DaemonClient:
    """Maintains the WebSocket to the daemon and bridges it to the channel."""

    def __init__(self) -> None:
        self._ws = None
        self._write_stream = None
        self._req_id = 0
        self._pending: dict[int, object] = {}  # req_id -> memory send stream
        self._registered_session: str | None = None
        self._connected_once = False  # did the current attempt establish a connection?
        # A foreground tool call sets this to break the reconnect backoff and force an
        # immediate attempt, so an active agent never waits out an idle-tuned long sleep.
        self._reconnect_now = anyio.Event()
        self._mode: str | None = (
            None  # None=detecting, "push" (channel), "pull" (catch_up)
        )
        self._buffer: dict[
            str, dict
        ] = {}  # notification id -> {content, meta}; held until acked
        # Wake-gating buffer (Phase B): message notifies the shim chose not to surface
        # (sub-threshold, or this session isn't a channel). Kept UNACKED so a reconnect
        # re-ships them; flushed when a surfacing message arrives or catch_up drains them.
        self._held: list[dict] = []  # [{id, content, meta}] in arrival order
        # Push-mode debounce: a burst of notifications is coalesced into one
        # channel event. Items wait here until the burst goes quiet (or caps out).
        self._pending_debounce: list[dict] = []  # [{id, content, meta}]
        self._debounce_event = anyio.Event()  # signalled on each push-mode arrival
        self._debounce_first: float | None = None  # monotonic ts of first pending item
        self._debounce_last: float | None = None  # monotonic ts of most recent arrival

    def attach_write_stream(self, write_stream) -> None:
        self._write_stream = write_stream

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def channel_label(self) -> str:
        return {
            "push": "active (delivered as channel events)",
            "pull": "inactive (not a channel) — call catch_up to pull updates",
        }.get(self._mode or "", "detecting")

    def delivery_hint(self) -> str:
        """How updates reach the agent, phrased for the current channel mode — so a
        subscribe confirmation doesn't promise <channel> events to a pull-mode session."""
        if self._mode == "pull":
            return (
                "This session was not loaded as a channel, so updates won't arrive "
                "automatically — call catch_up to retrieve them."
            )
        if self._mode == "push":
            return "Updates will arrive as <channel> events."
        # Detection not resolved yet (usually resolves to push); cover both.
        return (
            "Updates will arrive as <channel> events, or via the catch_up tool if "
            "this session turns out not to be a channel."
        )

    async def wait_connected(self, timeout: float = 8.0) -> bool:
        """Wait briefly for the connection (covers startup grace / reconnects).

        This is the foreground path — a tool the agent is actively invoking. Nudge the
        reconnect loop to retry NOW instead of waiting out its idle-tuned backoff sleep,
        then poll until connected or `timeout`. (Idle push delivery keeps the long
        backoff; only an actively-waiting caller forces the immediate attempt.)"""
        if self.connected:
            return True
        self._reconnect_now.set()  # an agent is actively waiting — break the backoff
        with anyio.move_on_after(timeout):
            while not self.connected:
                await anyio.sleep(0.1)
        return self.connected

    async def _send_raw(self, method: str, params: dict) -> None:
        if self._write_stream is None:
            return
        notification = JSONRPCNotification(jsonrpc="2.0", method=method, params=params)
        await self._write_stream.send(
            SessionMessage(message=JSONRPCMessage(notification))
        )

    async def _deliver_channel(self, content: str, meta: dict | None) -> None:
        params: dict[str, object] = {"content": content}
        if meta:
            params["meta"] = meta
        await self._send_raw(CHANNEL_METHOD, params)

    async def _ack(self, notification_id) -> None:
        ws = self._ws
        if ws is None or not notification_id:
            return
        try:
            await ws.send(json.dumps({"type": wsproto.ACK, "id": notification_id}))
        except (ConnectionClosed, WebSocketException):
            pass

    async def _handle_message_notify(
        self, notification_id, content: str, meta: dict
    ) -> None:
        """The receiver-side gate for agent messages (Phase B).

        Surface a message only when it is a channel (push) session AND the stamped
        ``level`` clears this session's ``threshold``. On surface, it goes to the
        debounce coalescer and the whole held backlog is flushed alongside it, and
        every surfaced/flushed message is acked. Otherwise the message is held UNACKED
        (sub-threshold, or this session can't push), to be drained by a later surfacing
        message or by catch_up. The ack-on-surface invariant — never ack on mere
        receipt — is what lets a reconnect re-ship anything still held."""
        if self._mode == "push" and messaging.should_surface(
            meta.get("level", "ambient"), meta.get("threshold", "direct")
        ):
            held, self._held = self._held, []
            for item in held:  # flush the quiet backlog in the same channel event
                await self._enqueue_push(item["id"], item["content"], item["meta"])
            await self._enqueue_push(notification_id, content, meta)
        else:
            self._held.append({"id": notification_id, "content": content, "meta": meta})

    async def _enqueue_push(
        self, notification_id, content: str, meta: dict | None
    ) -> None:
        """Deliver a push-mode notification, coalescing bursts via the debounce buffer.

        With debounce disabled (window <= 0) it is delivered (and acked) immediately;
        otherwise it is buffered and the debounce loop flushes the burst as one event.
        """
        if _debounce_window() <= 0:
            await self._deliver_channel(content, meta)
            await self._ack(notification_id)
            return
        now = time.monotonic()
        if self._debounce_first is None:
            self._debounce_first = now
        self._debounce_last = now
        self._pending_debounce.append(
            {"id": notification_id, "content": content, "meta": meta}
        )
        self._debounce_event.set()

    async def _flush_debounce(self) -> None:
        """Coalesce the pending push notifications into ONE channel event, then ack each.

        Content is the individual messages joined by a blank line; the coalesced meta
        carries the highest severity, kind="batch" when >1 item (else the lone item's
        kind), and count. The daemon's per-id acked-set is unchanged: every id is acked.
        """
        pending = self._pending_debounce
        self._pending_debounce = []
        self._debounce_first = None
        self._debounce_last = None
        if not pending:
            return
        content = "\n\n".join(item["content"] for item in pending)
        severity = (
            "high"
            if any((item["meta"] or {}).get("severity") == "high" for item in pending)
            else "info"
        )
        if len(pending) > 1:
            kind = "batch"
        else:
            kind = str((pending[0]["meta"] or {}).get("kind") or "notification")
        meta = {"severity": severity, "kind": kind, "count": str(len(pending))}
        await self._deliver_channel(content, meta)
        for item in pending:
            await self._ack(item["id"])

    async def debounce_loop(self) -> None:
        """Flush coalesced push notifications once a burst goes quiet (or hits the cap).

        Sleeps on an event each arrival signals, recomputing the flush deadline from the
        last arrival (quiet window) and the first pending item (max hold). No busy loop.
        """
        while True:
            await self._debounce_event.wait()
            self._debounce_event = anyio.Event()
            while self._pending_debounce:
                window = _debounce_window()
                deadline = min(
                    (self._debounce_last or 0.0) + window,
                    (self._debounce_first or 0.0) + _debounce_max(window),
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                fired = False
                with anyio.move_on_after(remaining):
                    await self._debounce_event.wait()
                    self._debounce_event = anyio.Event()
                    fired = True
                if not fired:  # quiet/max window elapsed with no new arrival
                    break
            await self._flush_debounce()

    async def apply_mode(self, detected: str) -> None:
        """Switch out of buffering once we know whether we're a channel.

        In push mode the PR/scheduled buffer is flushed straight to the debounce path,
        and any agent messages held during detection are re-run through the gate (so
        sub-threshold ones stay held rather than surfacing on mode resolution). In pull
        mode nothing is pushed — catch_up (always registered) is the only drain."""
        if detected == channel_detect.REGISTERED:
            self._mode = "push"
            for notification_id, payload in list(
                self._buffer.items()
            ):  # flush what we held, coalesced through the debounce path
                self._buffer.pop(notification_id, None)
                await self._enqueue_push(
                    notification_id, payload["content"], payload["meta"]
                )
            held, self._held = self._held, []
            for item in held:  # re-evaluate the gate now that we can push
                await self._handle_message_notify(
                    item["id"], item["content"], item["meta"]
                )
        else:  # skipped or unknown: err toward no silent loss
            self._mode = "pull"

    async def detect_and_apply(self) -> None:
        start = time.time()
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        detected = channel_detect.UNKNOWN
        while time.time() < start + CHANNEL_DETECT_TIMEOUT_SECONDS:
            detected = channel_detect.detect_channel_mode(
                SERVER_NAME, project_dir, newer_than=start - 5.0
            )
            if detected != channel_detect.UNKNOWN:
                break
            await anyio.sleep(CHANNEL_DETECT_POLL_SECONDS)
        await self.apply_mode(detected)

    async def drain_buffer(self) -> str:
        """Drain everything pending for this session and ack it: the pull-mode buffer
        (PR/scheduled notifications never pushed) and the wake-gating held buffer (agent
        messages below the threshold, or that arrived while not a channel)."""
        sections: list[str] = []
        if self._buffer:
            parts = [
                "Pending notifications (this session is not a channel, so they weren't pushed automatically):",
                "",
            ]
            for notification_id, payload in list(self._buffer.items()):
                parts.append(payload.get("content", ""))
                await self._ack(notification_id)
                self._buffer.pop(notification_id, None)
            sections.append("\n\n".join(parts))
        if self._held:
            held, self._held = self._held, []
            parts = ["Held agent messages (below your wake threshold):", ""]
            for item in held:
                parts.append(item.get("content", ""))
                await self._ack(item["id"])
            sections.append("\n\n".join(parts))
        if not sections:
            return "No pending notifications."
        return "\n\n".join(sections)

    async def request(self, payload: dict) -> dict:
        """Send a request to the daemon and await its correlated reply."""
        ws = self._ws
        if ws is None:
            raise ConnectionError("daemon not connected")
        self._req_id += 1
        req_id = self._req_id
        send_stream, receive_stream = anyio.create_memory_object_stream(1)
        self._pending[req_id] = send_stream
        try:
            await ws.send(json.dumps({**payload, "req_id": req_id}))
            with anyio.fail_after(REQUEST_TIMEOUT_SECONDS):
                return await receive_stream.receive()
        finally:
            self._pending.pop(req_id, None)

    async def run(self) -> None:
        await anyio.sleep(STARTUP_GRACE_SECONDS)
        failures = 0
        while True:
            started = time.monotonic()
            self._connected_once = False
            try:
                await self._connect_once()
            except (
                OSError,
                ConnectionClosed,
                InvalidURI,
                WebSocketException,
                ConnectionError,
            ):
                pass
            finally:
                self._ws = None
                self._registered_session = None
                # Drop un-acked pending items: the daemon resends unacked on
                # reconnect and they'll re-buffer, so don't deliver without acking.
                self._pending_debounce = []
                self._debounce_first = None
                self._debounce_last = None
                # Held messages are unacked, so the daemon re-ships them on reconnect
                # and they'll re-hold; clear to avoid duplicating the re-shipped copies.
                self._held = []
            stable = (
                self._connected_once
                and (time.monotonic() - started) >= RECONNECT_STABLE_SECONDS
            )
            failures = 0 if stable else min(failures + 1, _RECONNECT_MAX_FAILURES)
            # Interruptible backoff: wait the delay, but wake at once if a foreground
            # call nudges us — the long backoff stays for the idle/asleep case while an
            # actively-waiting agent reconnects immediately. anyio Events don't clear, so
            # swap in a fresh one after each wait.
            with anyio.move_on_after(_reconnect_delay(failures)):
                await self._reconnect_now.wait()
            self._reconnect_now = anyio.Event()

    async def _connect_once(self) -> None:
        headers = {"Authorization": f"Bearer {wsproto.token()}"}
        async with connect(
            wsproto.uri(), additional_headers=headers, open_timeout=5
        ) as ws:
            self._ws = ws
            self._connected_once = True
            await self._register(ws)
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._recv_loop, ws, tg)
                tg.start_soon(self._session_watch, ws, tg)

    async def _register(self, ws) -> None:
        session_id, _ = session_state.effective_session_id()
        self._registered_session = session_id
        await ws.send(json.dumps({"type": wsproto.REGISTER, "session_id": session_id}))

    async def _recv_loop(self, ws, tg) -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if msg.get("type") == wsproto.NOTIFY:
                    notification_id = msg.get("id")
                    meta = msg.get("meta") or {}
                    content = msg.get("content", "")
                    if meta.get("kind") == "message":  # Phase B: gated agent message
                        await self._handle_message_notify(
                            notification_id, content, meta
                        )
                    elif self._mode == "push":
                        await self._enqueue_push(notification_id, content, meta)
                    elif notification_id:  # detecting or pull: hold without acking
                        self._buffer[notification_id] = {
                            "content": content,
                            "meta": meta,
                        }
                else:
                    stream = self._pending.get(msg.get("req_id"))
                    if stream is not None:
                        await stream.send(msg)
        finally:
            tg.cancel_scope.cancel()  # connection ended; tear down and reconnect

    async def _session_watch(self, ws, tg) -> None:
        """Re-register if the session id changes (e.g. after a resume)."""
        while True:
            await anyio.sleep(SESSION_POLL_SECONDS)
            session_id, _ = session_state.effective_session_id()
            if session_id and session_id != self._registered_session:
                self._registered_session = session_id
                try:
                    await ws.send(
                        json.dumps({"type": wsproto.REGISTER, "session_id": session_id})
                    )
                except ConnectionClosed:
                    tg.cancel_scope.cancel()
                    return


DAEMON = DaemonClient()


def _daemon_unreachable_message() -> str:
    return (
        f"The notifications daemon is not reachable at {wsproto.uri()}. Start it "
        "(systemctl --user start notifications-daemon, or run "
        "notifications-daemon.py) and try again."
    )


@mcp.tool()
async def catch_up() -> str:
    """Retrieve and acknowledge pending notifications and quiet agent messages.

    Two things land here without interrupting you: agent messages that fell below your
    wake threshold (a plain channel post while you're at 'direct', say), and — if this
    session was NOT launched as a channel — every notification, since none can be pushed
    automatically. Call it periodically (and after long pauses) to drain both.
    """
    return await DAEMON.drain_buffer()


@mcp.tool()
def get_session_id() -> str:
    """Report the Claude Code session ID this relay is attached to.

    Prefers the id recorded by the SessionStart hook (correct across `/resume`),
    falling back to the CLAUDE_CODE_SESSION_ID environment variable.
    """
    session_id, source = session_state.effective_session_id()
    daemon = "connected" if DAEMON.connected else "disconnected"
    if not session_id:
        return (
            "No session ID available: neither the SessionStart hook's state file "
            f"nor {session_state.SESSION_ID_ENV_VAR} is set. Daemon: {daemon}."
        )
    return f"session_id={session_id} (source: {source}); daemon: {daemon}; channel: {DAEMON.channel_label}"


@mcp.tool()
async def schedule_test_notification(delay_seconds: int = 300) -> str:
    """Ask the daemon to schedule a callback notification `delay_seconds` out (default 300).

    The daemon persists it and delivers it as a channel event reporting this
    session's id, even across closing and reopening the session.
    """
    session_id, _ = session_state.effective_session_id()
    if not session_id:
        return "Cannot schedule: this relay does not yet know its session id."
    if not await DAEMON.wait_connected():
        return _daemon_unreachable_message()
    try:
        reply = await DAEMON.request(
            {
                "type": wsproto.SCHEDULE,
                "session_id": session_id,
                "delay_seconds": max(0, delay_seconds),
                "kind": "scheduled_test",
            }
        )
    except (ConnectionError, TimeoutError):
        return _daemon_unreachable_message()
    if reply.get("type") == wsproto.ERROR:
        return f"Daemon rejected the schedule request: {reply.get('error')}"
    return (
        f"Scheduled callback {reply.get('id')} for session {session_id} in "
        f"{max(0, delay_seconds)}s. The daemon will deliver it as a <channel> "
        "event, even across a restart."
    )


@mcp.tool()
async def list_scheduled_notifications() -> str:
    """List this session's pending notifications (queried from the daemon)."""
    session_id, _ = session_state.effective_session_id()
    if not session_id:
        return "Session id unknown; cannot list scheduled notifications."
    if not await DAEMON.wait_connected():
        return _daemon_unreachable_message()
    try:
        reply = await DAEMON.request({"type": wsproto.LIST, "session_id": session_id})
    except (ConnectionError, TimeoutError):
        return _daemon_unreachable_message()
    items = reply.get("items", [])
    if not items:
        return f"No scheduled notifications for session {session_id}."
    lines = [f"Scheduled notifications for session {session_id}:"]
    for item in items:
        lines.append(
            f"  {item.get('id')}  due_at={item.get('due_at')}  kind={item.get('kind')}"
        )
    return "\n".join(lines)


_PR_REF_RE = re.compile(r"^\s*([^/\s]+)/([^/#\s]+)#(\d+)\s*$")


async def _daemon_request(payload: dict) -> dict | str:
    """Common guard + request for the daemon-backed tools (PR and agent directory);
    returns the reply dict or a human-readable error string when unreachable."""
    if not await DAEMON.wait_connected():
        return _daemon_unreachable_message()
    try:
        return await DAEMON.request(payload)
    except (ConnectionError, TimeoutError):
        return _daemon_unreachable_message()


@mcp.tool()
async def subscribe_github_pr(pr: str) -> str:
    """Subscribe this session to notifications for a GitHub PR, given as org/repo#number.

    The daemon polls the PR (checks, reviews, comments, mergeability) and delivers
    updates as <channel> events with enough detail to act on. Subscriptions persist
    in the daemon; a merged PR auto-unsubscribes you.
    """
    match = _PR_REF_RE.match(pr or "")
    if not match:
        return "Invalid PR reference. Use org/repo#number, e.g. octocat/hello-world#42."
    session_id, _ = session_state.effective_session_id()
    if not session_id:
        return "Cannot subscribe: this relay does not yet know its session id."
    owner, repo, number = match.group(1), match.group(2), int(match.group(3))
    reply = await _daemon_request(
        {
            "type": wsproto.SUBSCRIBE_PR,
            "session_id": session_id,
            "owner": owner,
            "repo": repo,
            "number": number,
        }
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not subscribe to {owner}/{repo}#{number}: {reply.get('error')}"
    if reply.get("closed"):
        return f"{reply.get('pr')} is already closed/merged ({reply.get('summary')}); not subscribing."
    return f"Subscribed to {reply.get('pr')}. Current status: {reply.get('summary')}. {DAEMON.delivery_hint()}"


@mcp.tool()
async def unsubscribe_github_pr(pr: str) -> str:
    """Unsubscribe this session from a GitHub PR, given as org/repo#number."""
    match = _PR_REF_RE.match(pr or "")
    if not match:
        return "Invalid PR reference. Use org/repo#number, e.g. octocat/hello-world#42."
    session_id, _ = session_state.effective_session_id()
    if not session_id:
        return "Cannot unsubscribe: this relay does not yet know its session id."
    owner, repo, number = match.group(1), match.group(2), int(match.group(3))
    reply = await _daemon_request(
        {
            "type": wsproto.UNSUBSCRIBE_PR,
            "session_id": session_id,
            "owner": owner,
            "repo": repo,
            "number": number,
        }
    )
    if isinstance(reply, str):
        return reply
    return f"Unsubscribed from {reply.get('pr')}."


@mcp.tool()
async def list_github_pr_subscriptions() -> str:
    """List this session's active GitHub PR subscriptions (queried from the daemon)."""
    session_id, _ = session_state.effective_session_id()
    if not session_id:
        return "Session id unknown; cannot list PR subscriptions."
    reply = await _daemon_request(
        {"type": wsproto.LIST_PR_SUBSCRIPTIONS, "session_id": session_id}
    )
    if isinstance(reply, str):
        return reply
    items = reply.get("items", [])
    if not items:
        return "No active GitHub PR subscriptions for this session."
    lines = ["GitHub PR subscriptions:"]
    for item in items:
        state = " (merged)" if item.get("merged") else ""
        lines.append(f"  {item.get('pr')}{state}  pending={item.get('pending', 0)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# agent directory tools (Phase A)
# --------------------------------------------------------------------------- #


def _format_last_seen(last_seen: float) -> str:
    """A coarse, human 'how long ago' for an offline agent's last_seen epoch."""
    delta = max(0.0, time.time() - (last_seen or 0.0))
    if delta < 90:
        return f"~{int(delta)}s ago"
    minutes = delta / 60.0
    if minutes < 90:
        return f"~{int(round(minutes))}m ago"
    hours = minutes / 60.0
    if hours < 48:
        return f"~{int(round(hours))}h ago"
    return f"~{int(round(hours / 24.0))}d ago"


@mcp.tool()
async def register_agent(
    name: str,
    description: str = "",
    capabilities: str = "",
    working_dir: str = "",
    default_threshold: str | None = None,
) -> str:
    """Register this session in the shared agent directory under a self-chosen name.

    Binds a short, memorable name to this Claude Code session so other agents can
    find you (and, in later phases, message you). Names are lowercase kebab-case,
    2-64 chars (letters, digits, hyphens; no leading/trailing hyphen) — e.g.
    'frontend', 'pr-bot', 'reviewer-2'.

    Re-registering with the same name updates your profile in place. Registering a
    different name renames you, releasing the old one. A name held by another agent
    that is currently connected, or that disconnected only recently, is reserved and
    will be rejected; a long-abandoned name can be reclaimed.

    Args:
        name: your directory name (lowercase kebab-case, 2-64 chars).
        description: one line on who you are or what you're working on.
        capabilities: free-form note of what you can help with.
        working_dir: the repo or directory you're operating in.
        default_threshold: optional wake threshold ('all', 'direct', or 'urgent').
            Leave unset to keep your current setting (brand-new agents default to
            'direct'). See set_availability for what each level means.
    """
    session_id, _ = session_state.effective_session_id()
    if not session_id:
        return "Cannot register: this relay does not yet know its session id."
    payload: dict = {
        "type": wsproto.REGISTER_AGENT,
        "session_id": session_id,
        "name": name,
        "description": description,
        "capabilities": capabilities,
        "working_dir": working_dir,
    }
    # Only send default_threshold when explicitly provided: the registry treats a
    # missing value as "leave unchanged", so we never clobber a threshold the agent
    # set earlier via set_availability just by re-registering.
    if default_threshold is not None:
        payload["default_threshold"] = default_threshold
    reply = await _daemon_request(payload)
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not register as '{name}': {reply.get('error')}"
    agent = reply.get("agent") or {}
    threshold = agent.get("default_threshold", "direct")
    return (
        f"Registered as '{agent.get('name', name)}' (wake threshold: {threshold}). "
        "Other agents can find you with list_agents."
    )


@mcp.tool()
async def unregister_agent() -> str:
    """Leave the shared agent directory, releasing your name for others to claim.

    Removes this session's directory entry. Safe to call even if you were never
    registered. Your scheduled notifications and PR subscriptions are unaffected.
    """
    session_id, _ = session_state.effective_session_id()
    if not session_id:
        return "Cannot unregister: this relay does not yet know its session id."
    reply = await _daemon_request(
        {"type": wsproto.UNREGISTER_AGENT, "session_id": session_id}
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not unregister: {reply.get('error')}"
    agent = reply.get("agent")
    if not agent:
        return "You weren't registered as an agent."
    return (
        f"Unregistered '{agent.get('name')}'. The name is now free for other "
        "agents to claim."
    )


@mcp.tool()
async def list_agents() -> str:
    """List every agent in the shared directory, with live presence.

    Shows each agent's name, whether it is currently connected (or roughly how long
    ago it was last seen), its description, capabilities, working directory, and
    wake threshold. Use this to discover who else is around before coordinating.
    """
    session_id, _ = session_state.effective_session_id()
    reply = await _daemon_request(
        {"type": wsproto.LIST_AGENTS, "session_id": session_id}
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not list agents: {reply.get('error')}"
    agents = reply.get("agents", [])
    if not agents:
        return "Registered agents: (none registered)"
    lines = ["Registered agents:"]
    for agent in agents:
        if agent.get("connected"):
            presence = "connected"
        else:
            presence = (
                f"offline (last seen {_format_last_seen(agent.get('last_seen', 0.0))})"
            )
        lines.append(f"- {agent.get('name', '?')} — {presence}")
        if agent.get("description"):
            lines.append(f"    description: {agent['description']}")
        if agent.get("capabilities"):
            lines.append(f"    capabilities: {agent['capabilities']}")
        if agent.get("working_dir"):
            lines.append(f"    working dir: {agent['working_dir']}")
        lines.append(f"    wake threshold: {agent.get('default_threshold', 'direct')}")
    return "\n".join(lines)


@mcp.tool()
async def set_availability(default_threshold: str) -> str:
    """Set your wake threshold — how insistent a message must be to interrupt you.

    This is the 'do not disturb' knob for (Phase B) agent messaging. It is stored on
    your directory entry now; the message-gating that consumes it lands with
    messaging. You must be registered (see register_agent) first. Levels, from least
    to most strict:

        all     wake on every message to any channel you're in.
        direct  (default) wake only on messages that mention you, direct requests to
                you, or an @here addressed to a channel you're in.
        urgent  wake only on an @here.

    Args:
        default_threshold: one of 'all', 'direct', or 'urgent'.
    """
    session_id, _ = session_state.effective_session_id()
    if not session_id:
        return "Cannot set availability: this relay does not yet know its session id."
    reply = await _daemon_request(
        {
            "type": wsproto.SET_AVAILABILITY,
            "session_id": session_id,
            "default_threshold": default_threshold,
        }
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not set availability: {reply.get('error')}"
    agent = reply.get("agent") or {}
    return (
        f"Wake threshold set to '{agent.get('default_threshold', default_threshold)}'. "
        "Other agents see this in list_agents."
    )


# --------------------------------------------------------------------------- #
# agent messaging tools (Phase B): channels + DMs
# --------------------------------------------------------------------------- #
#
# Attention model (shared vocabulary across these tools):
#   intent    fyi (default, terminal — no reply expected) | question | request | reply.
#             Intent is display/reply semantics only; it does NOT change how loud a
#             message is.
#   severity  low | normal (default) | high. severity="high" is an @here: it wakes
#             every member regardless of their threshold.
#   mentions  a list of agent names — an @someone. A mentioned recipient is "addressed",
#             so the message wakes them at the 'direct' threshold even without @here.
#   threshold each recipient's bar (set via set_availability or per-context set_threshold):
#             all (every message) < direct (mentions / DMs / @here) < urgent (@here only).
# A message a recipient doesn't clear is held silently for their catch_up.


def _require_session(action: str) -> str | None:
    session_id, _ = session_state.effective_session_id()
    if not session_id:
        return f"Cannot {action}: this relay does not yet know its session id."
    return None


def _render_history(history: list[dict]) -> list[str]:
    lines: list[str] = []
    for msg in history:
        sender = msg.get("sender", "?")
        body = msg.get("body", "")
        marker = " [@here]" if msg.get("severity") == "high" else ""
        ordinal = msg.get("ordinal")
        handle = f"#{ordinal} " if ordinal else ""  # match the live #N rendering
        lines.append(f"    {handle}{sender}: {body}{marker}")
    return lines


@mcp.tool()
async def join_channel(
    channel: str, threshold: str | None = None, topic: str | None = None
) -> str:
    """Join (creating if needed) a shared channel so its messages reach you.

    Channels are communal: there is no owner, joining or posting creates one, and the
    member set is just whoever joined. Names are lowercase kebab-case (e.g. 'backend',
    'release-2'). Joining replays no history as notifications, but the reply shows the
    recent scrollback so you can catch up.

    Args:
        channel: the channel name (lowercase kebab-case).
        threshold: optional per-channel wake threshold ('all', 'direct', 'urgent') that
            overrides your global default just for this channel.
        topic: optional one-line topic to set if the channel is new/empty.
    """
    if err := _require_session("join channel"):
        return err
    try:
        messaging.validate_channel_name(channel or "")
        if threshold is not None:
            messaging.validate_threshold(threshold)
    except messaging.MessagingError as exc:
        return f"Could not join '{channel}': {exc}"
    session_id, _ = session_state.effective_session_id()
    payload: dict = {
        "type": wsproto.JOIN_CHANNEL,
        "session_id": session_id,
        "channel": channel,
    }
    if threshold is not None:
        payload["threshold"] = threshold
    if topic is not None:
        payload["topic"] = topic
    reply = await _daemon_request(payload)
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not join '{channel}': {reply.get('error')}"
    members = reply.get("members", [])
    lines = [
        f"Joined #{reply.get('channel', channel)} ({len(members)} member(s): "
        f"{', '.join(members) if members else 'just you'})."
    ]
    if reply.get("topic"):
        lines.append(f"Topic: {reply['topic']}")
    history = reply.get("history") or []
    if history:
        lines.append("Recent messages:")
        lines.extend(_render_history(history))
    return "\n".join(lines)


@mcp.tool()
async def leave_channel(channel: str) -> str:
    """Leave a channel you previously joined; its messages stop reaching you."""
    if err := _require_session("leave channel"):
        return err
    session_id, _ = session_state.effective_session_id()
    reply = await _daemon_request(
        {"type": wsproto.LEAVE_CHANNEL, "session_id": session_id, "channel": channel}
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not leave '{channel}': {reply.get('error')}"
    return f"Left #{channel}."


@mcp.tool()
async def post(
    channel: str,
    body: str,
    intent: str = "fyi",
    severity: str = "normal",
    mentions: list[str] | None = None,
) -> str:
    """Post a message to a channel (auto-joining it if you haven't already).

    Args:
        channel: the channel name (lowercase kebab-case). A name nobody else has joined
            means only you will see the post — the reply's member count flags that.
        body: the message text.
        intent: 'fyi' (default, terminal — no reply expected), 'question', 'request',
            or 'reply'. Intent conveys reply/display semantics, not loudness.
        severity: 'low', 'normal' (default), or 'high'. 'high' is an @here — it wakes
            every member no matter their threshold.
        mentions: a list of agent names to @-mention; each mentioned member is woken at
            the 'direct' threshold even without an @here.
    """
    if err := _require_session("post"):
        return err
    try:
        messaging.validate_channel_name(channel or "")
        messaging.validate_intent(intent)
        messaging.validate_severity(severity)
    except messaging.MessagingError as exc:
        return f"Could not post to '{channel}': {exc}"
    session_id, _ = session_state.effective_session_id()
    reply = await _daemon_request(
        {
            "type": wsproto.POST,
            "session_id": session_id,
            "channel": channel,
            "body": body,
            "intent": intent,
            "severity": severity,
            "mentions": list(mentions or []),
        }
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not post to '{channel}': {reply.get('error')}"
    ordinal = reply.get("ordinal")
    handle = f" (#{ordinal})" if ordinal else ""
    members = reply.get("members", 1)
    if members <= 1:
        return (
            f"Posted to #{channel}{handle}, but you're the only member — no one else "
            "will see it. Did you mean a different channel name?"
        )
    return f"Posted to #{channel}{handle} — {members} members will see it."


@mcp.tool()
async def dm(
    to: list[str], body: str, intent: str = "request", severity: str = "normal"
) -> str:
    """Send a direct message to one or more agents by name.

    A DM is a private thread keyed by its participant set, so messaging the same people
    again reuses one thread. Every recipient is 'addressed', so a DM wakes them at the
    'direct' threshold (and an @here — severity='high' — overrides even 'urgent').

    Args:
        to: a list of agent names to message (see list_agents for who's around).
        body: the message text.
        intent: 'request' (default), 'question', 'reply', or 'fyi' (terminal — no reply
            expected).
        severity: 'low', 'normal' (default), or 'high' (@here).
    """
    if err := _require_session("send DM"):
        return err
    try:
        messaging.validate_intent(intent)
        messaging.validate_severity(severity)
    except messaging.MessagingError as exc:
        return f"Could not send DM: {exc}"
    recipients = [to] if isinstance(to, str) else list(to or [])
    if not recipients:
        return "Could not send DM: name at least one recipient."
    session_id, _ = session_state.effective_session_id()
    reply = await _daemon_request(
        {
            "type": wsproto.DM,
            "session_id": session_id,
            "to": recipients,
            "body": body,
            "intent": intent,
            "severity": severity,
        }
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not send DM: {reply.get('error')}"
    ordinal = reply.get("ordinal")
    handle = f" (#{ordinal})" if ordinal else ""
    return (
        f"Sent DM to {', '.join(recipients)}{handle} — "
        f"{reply.get('members', 0)} in the thread."
    )


@mcp.tool()
async def set_threshold(context: str, threshold: str) -> str:
    """Set a per-channel/DM wake threshold, overriding your global default there.

    Args:
        context: the topic key from list_subscriptions (e.g. 'chan:backend').
        threshold: 'all' (every message), 'direct' (mentions / DMs / @here), or
            'urgent' (@here only).
    """
    if err := _require_session("set threshold"):
        return err
    try:
        messaging.validate_threshold(threshold or "")
    except messaging.MessagingError as exc:
        return f"Could not set threshold: {exc}"
    session_id, _ = session_state.effective_session_id()
    reply = await _daemon_request(
        {
            "type": wsproto.SET_THRESHOLD,
            "session_id": session_id,
            "context": context,
            "threshold": threshold,
        }
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not set threshold: {reply.get('error')}"
    return f"Wake threshold for {context} set to '{threshold}'."


@mcp.tool()
async def set_channel_topic(channel: str, topic: str) -> str:
    """Set a channel's one-line topic (the 'what this channel is for' description)."""
    if err := _require_session("set channel topic"):
        return err
    session_id, _ = session_state.effective_session_id()
    reply = await _daemon_request(
        {
            "type": wsproto.SET_CHANNEL_TOPIC,
            "session_id": session_id,
            "channel": channel,
            "topic": topic,
        }
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not set topic for '{channel}': {reply.get('error')}"
    return f"Topic for #{channel} set to: {topic}"


@mcp.tool()
async def list_channels() -> str:
    """List every channel the daemon knows about, with topic and member count."""
    session_id, _ = session_state.effective_session_id()
    reply = await _daemon_request(
        {"type": wsproto.LIST_CHANNELS, "session_id": session_id}
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not list channels: {reply.get('error')}"
    channels = reply.get("channels", [])
    if not channels:
        return "No channels exist yet. Use join_channel to create one."
    lines = ["Channels:"]
    for ch in sorted(channels, key=lambda c: c.get("name", "")):
        topic = f" — {ch['topic']}" if ch.get("topic") else ""
        lines.append(
            f"- #{ch.get('name', '?')} ({ch.get('members', 0)} member(s)){topic}"
        )
    return "\n".join(lines)


@mcp.tool()
async def list_subscriptions() -> str:
    """List the channels and DMs you're in, with your effective wake threshold for each."""
    if err := _require_session("list subscriptions"):
        return err
    session_id, _ = session_state.effective_session_id()
    reply = await _daemon_request(
        {"type": wsproto.LIST_SUBSCRIPTIONS, "session_id": session_id}
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not list subscriptions: {reply.get('error')}"
    subscriptions = reply.get("subscriptions", [])
    if not subscriptions:
        return "You're not in any channels or DMs yet."
    lines = ["Your channels and DMs:"]
    for sub in sorted(subscriptions, key=lambda s: s.get("context", "")):
        lines.append(
            f"- {sub.get('context', '?')} ({sub.get('kind', '?')}) — "
            f"wake threshold: {sub.get('threshold', 'direct')}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# receipts + reactions (Phase C)
# --------------------------------------------------------------------------- #


@mcp.tool()
async def react(message_id: str, reaction: str) -> str:
    """React to a message with a short, TERMINAL acknowledgment.

    A reaction acknowledges a message without asking for a reply, and NEVER wakes the
    recipient — it rides at 'ambient', so the author only sees it on their next turn,
    via catch_up, or in message_status. Prefer it over a courtesy "got it" post: it is
    the sanctioned way to say "received" without triggering a reply or an interruption.

    The reaction body is free-form but short — a single emoji or a terse token, no
    newlines (e.g. '👍', 'ack', 'done').

    Args:
        message_id: which message to react to. Pass the short '#N' handle shown on any
            message (e.g. '#147' — or just '147'), or the full id ('msg:chan:room:3').
        reaction: a short, single-line reaction body.
    """
    if err := _require_session("react"):
        return err
    try:
        messaging.validate_reaction(reaction or "")
    except messaging.MessagingError as exc:
        return f"Could not react: {exc}"
    session_id, _ = session_state.effective_session_id()
    reply = await _daemon_request(
        {
            "type": wsproto.REACT,
            "session_id": session_id,
            "target": message_id,
            "reaction": reaction,
        }
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not react to {message_id}: {reply.get('error')}"
    return f'Reacted "{reaction}" to {message_id} (terminal — no reply expected).'


@mcp.tool()
async def message_status(message_id: str) -> str:
    """Show who has received a message, who is still pending, and any reactions to it.

    'Delivered' means the message reached that agent's context — it was surfaced, or
    drained via catch_up — NOT that they read, understood, or acted on it. A recipient
    holding the message below their wake threshold reads as 'pending' until they
    catch_up. The message's own author is never counted (it pre-acks its own post).

    Args:
        message_id: which message to inspect. Pass the short '#N' handle shown on any
            message (e.g. '#147' — or just '147'), or the full id ('msg:chan:room:3').
    """
    if err := _require_session("check message status"):
        return err
    session_id, _ = session_state.effective_session_id()
    reply = await _daemon_request(
        {
            "type": wsproto.MESSAGE_STATUS,
            "session_id": session_id,
            "target": message_id,
        }
    )
    if isinstance(reply, str):
        return reply
    if reply.get("type") == wsproto.ERROR:
        return f"Could not get status for {message_id}: {reply.get('error')}"
    delivered = reply.get("delivered", [])
    pending = reply.get("pending", [])
    reactions = reply.get("reactions", [])
    total = len(delivered) + len(pending)
    parts = [f"Delivered to {len(delivered)} of {total}"]
    if delivered:
        parts[0] += ": " + ", ".join(delivered)
    if pending:
        parts.append("pending: " + ", ".join(pending))
    if reactions:
        parts.append(
            "reactions: "
            + ", ".join(f"{r.get('reaction')} {r.get('by')}" for r in reactions)
        )
    return (
        f"{message_id} — {'; '.join(parts)}. (Delivered means the message reached the "
        "agent's context, not that it was read or acted on.)"
    )


async def _serve() -> None:
    server = mcp._mcp_server
    init_options = server.create_initialization_options(
        notification_options=NotificationOptions(tools_changed=True),
        experimental_capabilities={"claude/channel": {}},
    )
    async with stdio_server() as (read_stream, write_stream):
        DAEMON.attach_write_stream(write_stream)
        async with anyio.create_task_group() as tg:
            tg.start_soon(DAEMON.run)
            tg.start_soon(DAEMON.detect_and_apply)  # decide push vs catch_up
            tg.start_soon(DAEMON.debounce_loop)  # coalesce push-mode bursts
            await server.run(read_stream, write_stream, init_options)
            tg.cancel_scope.cancel()  # transport closed: stop the daemon client


if __name__ == "__main__":
    anyio.run(_serve)
