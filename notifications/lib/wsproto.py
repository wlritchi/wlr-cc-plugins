# vim: filetype=python
"""Shared wire protocol between the notifications daemon and the stdio MCP relay.

Messages are JSON text frames over a localhost WebSocket. Request/response
messages carry a `req_id` the daemon echoes back so the relay can correlate
replies; `notify` messages are daemon-initiated and carry no `req_id`.

stdlib only (imported by both the asyncio daemon and the anyio MCP server).
"""

import os
import secrets
import time
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8137

# relay -> daemon
REGISTER = "register"  # {session_id}            attach this connection to a session
SCHEDULE = "schedule"  # {req_id, session_id, delay_seconds, kind}
ACK = "ack"  # {id}                              a delivered notification was handled
LIST = "list"  # {req_id, session_id}
SUBSCRIBE_PR = "subscribe_pr"  # {req_id, session_id, owner, repo, number}
UNSUBSCRIBE_PR = "unsubscribe_pr"  # {req_id, session_id, owner, repo, number}
LIST_SUBSCRIPTIONS = "list_subscriptions"  # {req_id, session_id}

# daemon -> relay
NOTIFY = "notify"  # {id, content, meta}          deliver this to the agent, then ack
SCHEDULED = "scheduled"  # {req_id, id, due_at}
LIST_RESULT = "list_result"  # {req_id, items}
SUBSCRIBED = "subscribed"  # {req_id, pr, summary, merged, closed}
UNSUBSCRIBED = "unsubscribed"  # {req_id, pr}
SUBSCRIPTIONS_RESULT = "subscriptions_result"  # {req_id, items}
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


def _data_dir() -> Path:
    base = os.environ.get("NOTIFICATIONS_DATA_DIR")
    return Path(base) if base else Path.home() / ".claude" / "notifications"


def token() -> str:
    """Shared secret authenticating relay->daemon WebSocket connections.

    Returns NOTIFICATIONS_TOKEN if set; otherwise reads (or atomically creates) a
    token file at <NOTIFICATIONS_DATA_DIR>/token, mode 0600. The daemon and relay
    compute the same path from NOTIFICATIONS_DATA_DIR, so a local setup agrees with
    zero configuration. Creation is racy-safe: whoever wins the O_EXCL create writes
    the secret, the other reads it (retrying briefly while the file is still empty).
    """
    env = os.environ.get("NOTIFICATIONS_TOKEN")
    if env:
        return env
    path = _data_dir() / "token"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _read_token(path)
    try:
        value = secrets.token_urlsafe(32)
        os.write(fd, value.encode())
    finally:
        os.close(fd)
    return value


def _read_token(path: Path) -> str:
    # The O_EXCL create winner may not have written the secret yet; the file exists
    # but is momentarily empty. Retry briefly rather than return a blank token.
    for _ in range(100):
        try:
            value = path.read_text().strip()
        except OSError:
            value = ""
        if value:
            return value
        time.sleep(0.01)
    return path.read_text().strip()
