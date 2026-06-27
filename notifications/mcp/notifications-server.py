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
import session_state  # noqa: E402
import wsproto  # noqa: E402

CHANNEL_METHOD = "notifications/claude/channel"
TOOLS_CHANGED_METHOD = "notifications/tools/list_changed"
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
    "This plugin delivers notifications (scheduled callbacks and GitHub PR updates) "
    "through the Claude Code channels feature. Events normally arrive on their own as "
    '<channel source="notifications" ...> messages; when one arrives, surface it to the '
    "user. If a `catch_up` tool is available, this session was NOT loaded as a channel, "
    "so notifications are not pushed automatically — call `catch_up` periodically (and "
    "after long pauses) to retrieve pending updates."
)

mcp = FastMCP("notifications", instructions=INSTRUCTIONS)

# Set once the relay learns it is not a channel and registers the catch_up tool.
_catch_up_registered = False


class DaemonClient:
    """Maintains the WebSocket to the daemon and bridges it to the channel."""

    def __init__(self) -> None:
        self._ws = None
        self._write_stream = None
        self._req_id = 0
        self._pending: dict[int, object] = {}  # req_id -> memory send stream
        self._registered_session: str | None = None
        self._connected_once = False  # did the current attempt establish a connection?
        self._mode: str | None = (
            None  # None=detecting, "push" (channel), "pull" (catch_up)
        )
        self._buffer: dict[
            str, dict
        ] = {}  # notification id -> {content, meta}; held until acked
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
        """Wait briefly for the connection (covers startup grace / reconnects)."""
        if self.connected:
            return True
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
        """Switch out of buffering once we know whether we're a channel."""
        if detected == channel_detect.REGISTERED:
            self._mode = "push"
            for notification_id, payload in list(
                self._buffer.items()
            ):  # flush what we held, coalesced through the debounce path
                self._buffer.pop(notification_id, None)
                await self._enqueue_push(
                    notification_id, payload["content"], payload["meta"]
                )
        else:  # skipped or unknown: err toward no silent loss
            self._mode = "pull"
            global _catch_up_registered
            if not _catch_up_registered:
                _catch_up_registered = True
                mcp.add_tool(catch_up)
                await self._send_raw(TOOLS_CHANGED_METHOD, {})

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
        if not self._buffer:
            return "No pending notifications."
        lines = [
            "Pending notifications (this session is not a channel, so they weren't pushed automatically):",
            "",
        ]
        for notification_id, payload in list(self._buffer.items()):
            lines.append(payload.get("content", ""))
            await self._ack(notification_id)
            self._buffer.pop(notification_id, None)
        return "\n\n".join(lines)

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
            stable = (
                self._connected_once
                and (time.monotonic() - started) >= RECONNECT_STABLE_SECONDS
            )
            failures = 0 if stable else min(failures + 1, _RECONNECT_MAX_FAILURES)
            await anyio.sleep(_reconnect_delay(failures))

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
                    if self._mode == "push":
                        await self._enqueue_push(
                            notification_id, msg.get("content", ""), msg.get("meta")
                        )
                    elif notification_id:  # detecting or pull: hold without acking
                        self._buffer[notification_id] = {
                            "content": msg.get("content", ""),
                            "meta": msg.get("meta"),
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


async def catch_up() -> str:
    """Retrieve pending notifications for this session.

    This tool only exists because the session was NOT launched as a channel, so
    notifications can't be pushed automatically — they're buffered here. Call it
    periodically (and after long pauses) to pull and acknowledge pending updates.
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
        {"type": wsproto.LIST_SUBSCRIPTIONS, "session_id": session_id}
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
