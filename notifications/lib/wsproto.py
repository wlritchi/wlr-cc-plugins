# vim: filetype=python
"""Shared wire protocol between the notifications daemon and the stdio MCP relay.

Messages are JSON text frames over a localhost WebSocket. Request/response
messages carry a `req_id` the daemon echoes back so the relay can correlate
replies; `notify` messages are daemon-initiated and carry no `req_id`.

stdlib only (imported by both the asyncio daemon and the anyio MCP server).
"""

import os

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8137

# relay -> daemon
REGISTER = "register"  # {session_id}            attach this connection to a session
SCHEDULE = "schedule"  # {req_id, session_id, delay_seconds, kind}
ACK = "ack"  # {id}                              a delivered notification was handled
LIST = "list"  # {req_id, session_id}

# daemon -> relay
NOTIFY = "notify"  # {id, content, meta}          deliver this to the agent, then ack
SCHEDULED = "scheduled"  # {req_id, id, due_at}
LIST_RESULT = "list_result"  # {req_id, items}
ERROR = "error"  # {req_id, error}


def host() -> str:
    return os.environ.get("NOTIFICATIONS_WS_HOST", DEFAULT_HOST)


def port() -> int:
    raw = os.environ.get("NOTIFICATIONS_WS_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_PORT


def uri() -> str:
    return f"ws://{host()}:{port()}"
