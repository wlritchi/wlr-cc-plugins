#!/usr/bin/env -S uv run -qs
# vim: filetype=python
"""
Notifications daemon

A persistent, single-instance WebSocket server that owns the notification
schedule for all Claude sessions on this machine. The per-session stdio MCP
relays (see ../mcp/notifications-server.py) connect to it over localhost,
register their session id, forward the agent's schedule requests, receive
due notifications, and acknowledge them.

Responsibilities (everything stateful lives here, not in the relays):
  - persist scheduled callbacks to disk, keyed by session id (../lib/scheduler.py)
  - per connection, dispatch callbacks that are due for that session
  - hold undelivered/unacked callbacks until a relay for that session connects,
    so a callback that comes due while nothing is open is delivered on reconnect
  - delete a callback only once the relay acks it (at-least-once delivery)

Run manually:   uv run -qs notifications-daemon.py
Or via systemd:  see ./README.md  (systemctl --user)

Config (env):  NOTIFICATIONS_WS_HOST (default 127.0.0.1)
               NOTIFICATIONS_WS_PORT (default 8137)
               NOTIFICATIONS_DATA_DIR (default ~/.claude/notifications)
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets"]
# ///

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import scheduler  # noqa: E402
import wsproto  # noqa: E402

# How often each connection re-checks the store for newly-due callbacks.
DISPATCH_POLL_SECONDS = 2.0
# A callback this far past due was almost certainly held while nothing was
# connected, rather than just late by a poll tick — label it as recovered.
RECOVERED_THRESHOLD_SECONDS = 30


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_notification(
    entry: dict, session_id: str, now: float
) -> tuple[str, dict[str, str]]:
    """The notification body the daemon owns and the relay relays verbatim."""
    due = float(entry.get("due_at", now))
    created_for = entry.get("session_id", "?")
    late = int(now - due)
    recovered = (
        " (recovered after restart)" if late >= RECOVERED_THRESHOLD_SECONDS else ""
    )
    content = (
        f"Scheduled notification fired{recovered}. callback_id={entry.get('id')} "
        f"session_id={session_id} (scheduled for {created_for}) "
        f"due_at={_iso(due)} now={_iso(now)} late_by={late}s"
    )
    meta = {
        "severity": "info",
        "kind": str(entry.get("kind", "scheduled")),
        "callback_id": str(entry.get("id", "")),
    }
    return content, meta


class Connection:
    """State for one connected relay (one Claude session)."""

    def __init__(self, websocket) -> None:
        self.ws = websocket
        self.session_id: str | None = None
        self.inflight: set[str] = set()  # sent, awaiting ack


async def _dispatch_loop(conn: Connection) -> None:
    """Push due callbacks for this connection's session until it closes."""
    while True:
        session_id = conn.session_id
        if session_id:
            now = time.time()
            for entry in scheduler.due_callbacks(session_id, now):
                callback_id = str(entry.get("id", ""))
                if not callback_id or callback_id in conn.inflight:
                    continue
                content, meta = _build_notification(entry, session_id, now)
                try:
                    await conn.ws.send(
                        json.dumps(
                            {
                                "type": wsproto.NOTIFY,
                                "id": callback_id,
                                "content": content,
                                "meta": meta,
                            }
                        )
                    )
                except ConnectionClosed:
                    return
                conn.inflight.add(callback_id)
        await asyncio.sleep(DISPATCH_POLL_SECONDS)


async def _handle(websocket) -> None:
    conn = Connection(websocket)
    dispatch_task: asyncio.Task | None = None
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            kind = msg.get("type")

            if kind == wsproto.REGISTER:
                conn.session_id = msg.get("session_id") or None
                if dispatch_task is None and conn.session_id:
                    dispatch_task = asyncio.create_task(_dispatch_loop(conn))

            elif kind == wsproto.SCHEDULE:
                session_id = msg.get("session_id") or conn.session_id
                if not session_id:
                    await _send(websocket, wsproto.ERROR, msg, error="no session id")
                    continue
                delay = max(0, int(msg.get("delay_seconds", 300)))
                callback_id = scheduler.schedule(
                    session_id,
                    time.time() + delay,
                    kind=str(msg.get("kind", "scheduled")),
                )
                await _send(
                    websocket,
                    wsproto.SCHEDULED,
                    msg,
                    id=callback_id,
                    due_at=time.time() + delay,
                )

            elif kind == wsproto.ACK:
                callback_id = msg.get("id")
                if conn.session_id and callback_id:
                    scheduler.delete(conn.session_id, callback_id)
                    conn.inflight.discard(callback_id)

            elif kind == wsproto.LIST:
                session_id = msg.get("session_id") or conn.session_id
                items = (
                    [
                        {
                            "id": e.get("id"),
                            "due_at": e.get("due_at"),
                            "kind": e.get("kind"),
                        }
                        for e in scheduler.pending(session_id)
                    ]
                    if session_id
                    else []
                )
                await _send(websocket, wsproto.LIST_RESULT, msg, items=items)
    except ConnectionClosed:
        pass
    finally:
        if dispatch_task is not None:
            dispatch_task.cancel()


async def _send(websocket, msg_type: str, request: dict, **fields: object) -> None:
    payload: dict[str, object] = {"type": msg_type, **fields}
    if "req_id" in request:
        payload["req_id"] = request["req_id"]
    try:
        await websocket.send(json.dumps(payload))
    except ConnectionClosed:
        pass


async def main() -> None:
    host, port = wsproto.host(), wsproto.port()
    try:
        server_cm = serve(_handle, host, port)
    except OSError as exc:  # pragma: no cover - defensive
        print(
            f"notifications daemon: cannot bind {host}:{port}: {exc}", file=sys.stderr
        )
        raise SystemExit(1) from exc
    try:
        async with server_cm:
            print(
                f"notifications daemon listening on ws://{host}:{port}", file=sys.stderr
            )
            await asyncio.get_running_loop().create_future()  # run forever
    except OSError as exc:
        print(
            f"notifications daemon: cannot bind {host}:{port} (already running?): {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
