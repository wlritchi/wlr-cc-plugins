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
LIST_PR_SUBSCRIPTIONS = "list_pr_subscriptions"  # {req_id, session_id}
# agent directory (Phase A)
REGISTER_AGENT = "register_agent"  # {req_id, session_id, name, description?, capabilities?, working_dir?, default_threshold?}
UNREGISTER_AGENT = "unregister_agent"  # {req_id, session_id}
LIST_AGENTS = "list_agents"  # {req_id, session_id}
SET_AVAILABILITY = "set_availability"  # {req_id, session_id, default_threshold}
# agent messaging (Phase B) — relay -> daemon
JOIN_CHANNEL = "join_channel"  # {req_id, session_id, channel, threshold?, topic?}
LEAVE_CHANNEL = "leave_channel"  # {req_id, session_id, channel}
POST = "post"  # {req_id, session_id, channel, body, intent?, severity?, mentions?}
DM = "dm"  # {req_id, session_id, to: [name...], body, intent?, severity?}
SET_THRESHOLD = "set_threshold"  # {req_id, session_id, context, threshold}
SET_CHANNEL_TOPIC = "set_channel_topic"  # {req_id, session_id, channel, topic}
LIST_CHANNELS = "list_channels"  # {req_id, session_id}
LIST_SUBSCRIPTIONS = "list_subscriptions"  # {req_id, session_id}
# receipts + reactions (Phase C) — relay -> daemon
REACT = "react"  # {req_id, session_id, target, reaction}
MESSAGE_STATUS = "message_status"  # {req_id, session_id, target}

# daemon -> relay
# NOTIFY meta optionally carries message-gating fields when kind=="message":
#   {kind:"message", context, level, threshold, from, intent, severity, mentions}.
NOTIFY = "notify"  # {id, content, meta}          deliver this to the agent, then ack
SCHEDULED = "scheduled"  # {req_id, id, due_at}
LIST_RESULT = "list_result"  # {req_id, items}
SUBSCRIBED = "subscribed"  # {req_id, pr, summary, merged, closed}
UNSUBSCRIBED = "unsubscribed"  # {req_id, pr}
SUBSCRIPTIONS_RESULT = "subscriptions_result"  # {req_id, items}
AGENT_OK = "agent_ok"  # {req_id, agent}          resulting record dict (or null/{name} for unregister)
AGENT_LIST = "agent_list"  # {req_id, agents}      [record-dict + "connected": bool]
# agent messaging (Phase B) — daemon -> relay (AGENT_OK acks leave/set_threshold/
# set_channel_topic; ERROR reports not-registered/unknown-name/invalid args).
CHANNEL_JOINED = (
    "channel_joined"  # {req_id, channel, members, topic, history: [msg...]}
)
POSTED = "posted"  # {req_id, id, ordinal, context, members}
CHANNEL_LIST = (
    "channel_list"  # {req_id, channels: [{name, topic, members, last_activity}]}
)
SUBSCRIPTION_LIST = (
    "subscription_list"  # {req_id, subscriptions: [{context, kind, threshold}]}
)
# receipts + reactions (Phase C) — daemon -> relay (AGENT_OK acks a react, carrying
# the reaction's own id; ERROR reports not-a-member / unknown message / invalid reaction).
MESSAGE_STATUS_RESULT = "message_status_result"  # {req_id, delivered: [name...], pending: [name...], reactions: [{by, reaction}]}
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
    """The URL the relay connects to. A full ``NOTIFICATIONS_WS_URL`` override wins
    when set — e.g. ``wss://notifications.example.com`` for a remote daemon behind a
    TLS-terminating ingress (scheme/host/port/path all honored). Otherwise it is built
    from NOTIFICATIONS_WS_HOST/PORT. Only the relay (client) consults this; the daemon
    binds via host()/port() and stays plain ``ws`` inside the pod (TLS terminates at
    the ingress)."""
    return os.environ.get("NOTIFICATIONS_WS_URL") or f"ws://{host()}:{port()}"


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
