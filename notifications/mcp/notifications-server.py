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
  - receive notifications from the daemon and deliver them to the agent as
    Claude Code channel events (notifications/claude/channel)
  - acknowledge delivered notifications back to the daemon
  - forward the agent's schedule tool call to the daemon

The daemon must be running (it is started manually or via systemd --user; this
relay never spawns it). If it isn't reachable, the relay keeps retrying and the
schedule/list tools report it as unavailable.

To receive the channel events the session must be launched with the channel
enabled, e.g. `claude --dangerously-load-development-channels plugin:notifications@wlr-cc-plugins`.

Run directly: ./notifications-server.py
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp", "anyio", "websockets"]
# ///

import json
import random
import re
import sys
import time
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import session_state  # noqa: E402
import wsproto  # noqa: E402

CHANNEL_METHOD = "notifications/claude/channel"
SESSION_POLL_SECONDS = 5.0
# Settle time before connecting, so a recovered (past-due) event isn't pushed
# into the channel before the client has finished the MCP/channel handshake.
STARTUP_GRACE_SECONDS = 3.0
REQUEST_TIMEOUT_SECONDS = 10.0

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
    "This plugin delivers scheduled notifications through the Claude Code "
    'channels feature. Events arrive as <channel source="notifications" ...> '
    "with a body reporting the callback id and the session id it was scheduled "
    "for. These are proof-of-concept demo notifications; when one arrives, "
    "surface it to the user."
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

    def attach_write_stream(self, write_stream) -> None:
        self._write_stream = write_stream

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def wait_connected(self, timeout: float = 8.0) -> bool:
        """Wait briefly for the connection (covers startup grace / reconnects)."""
        if self.connected:
            return True
        with anyio.move_on_after(timeout):
            while not self.connected:
                await anyio.sleep(0.1)
        return self.connected

    async def _deliver_channel(self, content: str, meta: dict | None) -> None:
        params: dict[str, object] = {"content": content}
        if meta:
            params["meta"] = meta
        notification = JSONRPCNotification(
            jsonrpc="2.0", method=CHANNEL_METHOD, params=params
        )
        await self._write_stream.send(
            SessionMessage(message=JSONRPCMessage(notification))
        )

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
            stable = (
                self._connected_once
                and (time.monotonic() - started) >= RECONNECT_STABLE_SECONDS
            )
            failures = 0 if stable else min(failures + 1, _RECONNECT_MAX_FAILURES)
            await anyio.sleep(_reconnect_delay(failures))

    async def _connect_once(self) -> None:
        async with connect(wsproto.uri(), open_timeout=5) as ws:
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
                    await self._deliver_channel(msg.get("content", ""), msg.get("meta"))
                    await ws.send(
                        json.dumps({"type": wsproto.ACK, "id": msg.get("id")})
                    )
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
    return f"session_id={session_id} (source: {source}); daemon: {daemon}"


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


async def _pr_request(payload: dict) -> dict | str:
    """Common guard + request for the PR tools; returns the reply or an error string."""
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
    reply = await _pr_request(
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
    return f"Subscribed to {reply.get('pr')}. Current status: {reply.get('summary')}. Updates will arrive as <channel> events."


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
    reply = await _pr_request(
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
    reply = await _pr_request(
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


async def _serve() -> None:
    server = mcp._mcp_server
    init_options = server.create_initialization_options(
        experimental_capabilities={"claude/channel": {}},
    )
    async with stdio_server() as (read_stream, write_stream):
        DAEMON.attach_write_stream(write_stream)
        async with anyio.create_task_group() as tg:
            tg.start_soon(DAEMON.run)
            await server.run(read_stream, write_stream, init_options)
            tg.cancel_scope.cancel()  # transport closed: stop the daemon client


if __name__ == "__main__":
    anyio.run(_serve)
