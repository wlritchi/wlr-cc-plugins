#!/usr/bin/env -S uv run -qs
# vim: filetype=python
"""
Notifications MCP Server

Groundwork for a Claude Code notification framework, built around the Claude
Code "channels" feature (https://code.claude.com/docs/en/channels.md): this
stdio server declares the experimental `claude/channel` capability and pushes
`notifications/claude/channel` events into the session that loaded it.

Proof-of-concept capability: schedule a callback notification N seconds out (5
minutes by default). The callback is persisted to disk keyed by session id, so
if every Claude session is closed and reopened, each restarted server recovers
its session id (via the SessionStart hook's state file, see
../lib/session_state.py), reloads its scheduled callbacks, and dispatches any
that are now due. The persistence and the notification body are throwaway demos
to prove channel delivery + session-id recovery.

To receive the events the session must be launched with the channel enabled,
e.g. `claude --dangerously-load-development-channels plugin:notifications@wlr-cc-plugins`
during the channels research preview.

Run directly: ./notifications-server.py
Or via uv:    uv run -qs notifications-server.py
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp", "anyio"]
# ///

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import scheduler  # noqa: E402
import session_state  # noqa: E402

# How often the dispatcher re-checks the session-id state file and the store.
POLL_INTERVAL_SECONDS = 5.0
# Brief settle time before the first dispatch so we don't push a recovered
# (past-due) event before the client has finished the MCP/channel handshake.
STARTUP_GRACE_SECONDS = 3.0
DEFAULT_DELAY_SECONDS = 300

CHANNEL_METHOD = "notifications/claude/channel"

INSTRUCTIONS = (
    "This plugin delivers scheduled notifications through the Claude Code "
    'channels feature. Events arrive as <channel source="notifications" ...> '
    "with a body reporting the callback id and the session id it was scheduled "
    "for. These are proof-of-concept demo notifications; when one arrives, "
    "surface it to the user."
)

mcp = FastMCP("notifications", instructions=INSTRUCTIONS)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@mcp.tool()
def get_session_id() -> str:
    """Report the Claude Code session ID this notifications server is attached to.

    Prefers the id recorded by the SessionStart hook (correct across `/resume`),
    falling back to the CLAUDE_CODE_SESSION_ID environment variable.
    """
    session_id, source = session_state.effective_session_id()
    if not session_id:
        return (
            "No session ID available: neither the SessionStart hook's state file "
            f"nor {session_state.SESSION_ID_ENV_VAR} is set."
        )
    return f"session_id={session_id} (source: {source})"


@mcp.tool()
def schedule_test_notification(delay_seconds: int = DEFAULT_DELAY_SECONDS) -> str:
    """Schedule a callback notification to fire `delay_seconds` from now (default 300).

    The callback is persisted to disk and will be delivered as a channel event
    even if this session is closed and later reopened. The event reports this
    server's session id, demonstrating session-id recovery.
    """
    session_id, _ = session_state.effective_session_id()
    if not session_id:
        return (
            "Cannot schedule: this server does not yet know its session id (the "
            "SessionStart hook may not have run). Try get_session_id first."
        )
    due_at = time.time() + max(0, delay_seconds)
    callback_id = scheduler.schedule(session_id, due_at, kind="scheduled_test")
    return (
        f"Scheduled callback {callback_id} for session {session_id}; due "
        f"{_iso(due_at)} (in {max(0, delay_seconds)}s). It will arrive as a "
        "<channel> event from this plugin, even across a restart."
    )


@mcp.tool()
def list_scheduled_notifications() -> str:
    """List this session's pending (not-yet-delivered) scheduled notifications."""
    session_id, _ = session_state.effective_session_id()
    if not session_id:
        return "Session id unknown; cannot list scheduled notifications."
    entries = scheduler.pending(session_id)
    if not entries:
        return f"No scheduled notifications for session {session_id}."
    now = time.time()
    lines = [f"Scheduled notifications for session {session_id}:"]
    for entry in entries:
        due = float(entry.get("due_at", 0))
        rel = int(due - now)
        when = "overdue" if rel <= 0 else f"in {rel}s"
        lines.append(f"  {entry['id']}  due {_iso(due)} ({when})")
    return "\n".join(lines)


def _dispatch_content(entry: dict, session_id: str, now: float) -> str:
    due = float(entry.get("due_at", now))
    created_for = entry.get("session_id", "?")
    late = int(now - due)
    recovered = " (recovered after restart)" if late >= POLL_INTERVAL_SECONDS else ""
    return (
        f"Scheduled notification fired{recovered}. callback_id={entry.get('id')} "
        f"session_id={session_id} (scheduled for {created_for}) "
        f"due_at={_iso(due)} now={_iso(now)} late_by={late}s"
    )


async def _send_channel_event(
    write_stream, content: str, meta: dict[str, str] | None = None
) -> None:
    params: dict[str, object] = {"content": content}
    if meta:
        params["meta"] = meta
    notification = JSONRPCNotification(
        jsonrpc="2.0", method=CHANNEL_METHOD, params=params
    )
    await write_stream.send(SessionMessage(message=JSONRPCMessage(notification)))


async def _run_dispatcher(write_stream) -> None:
    """Poll the session-id state file and the store; deliver due callbacks.

    Polling (rather than on-demand) is what makes recovery proactive: a freshly
    restarted server learns its real session id and fires past-due callbacks
    without waiting for the agent to call a tool.
    """
    await anyio.sleep(STARTUP_GRACE_SECONDS)
    while True:
        try:
            session_id, _ = session_state.effective_session_id()
            if session_id:
                now = time.time()
                for entry in scheduler.due_callbacks(session_id, now):
                    meta = {
                        "severity": "info",
                        "kind": str(entry.get("kind", "scheduled")),
                        "callback_id": str(entry.get("id", "")),
                    }
                    await _send_channel_event(
                        write_stream, _dispatch_content(entry, session_id, now), meta
                    )
                    scheduler.mark_dispatched(entry)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            return  # transport closed; stop dispatching
        except Exception:
            pass  # best-effort: never let dispatch errors kill the loop
        await anyio.sleep(POLL_INTERVAL_SECONDS)


async def _serve() -> None:
    server = mcp._mcp_server
    init_options = server.create_initialization_options(
        experimental_capabilities={"claude/channel": {}},
    )
    async with stdio_server() as (read_stream, write_stream):
        async with anyio.create_task_group() as tg:
            tg.start_soon(_run_dispatcher, write_stream)
            await server.run(read_stream, write_stream, init_options)
            tg.cancel_scope.cancel()  # transport closed: stop the dispatcher


if __name__ == "__main__":
    anyio.run(_serve)
