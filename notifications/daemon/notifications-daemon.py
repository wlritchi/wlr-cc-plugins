#!/usr/bin/env -S uv run -qs
# vim: filetype=python
"""
Notifications daemon

A persistent, single-instance WebSocket server that owns notification state for
all Claude sessions on this machine. Per-session stdio MCP relays connect over
localhost, register their session id, and exchange notifications.

It provides two capabilities:
  - scheduled one-shot callbacks (proof-of-concept; ../lib/scheduler.py)
  - GitHub PR monitoring (../lib/pr_monitor.py): polls subscribed PRs for checks,
    reviews, comments and mergeability, and pushes rich notifications. Polling
    cadence backs off but is capped during business hours (../lib/pr_schedule.py).
    Updates are cached (content-addressed event ids) and each subscriber tracks
    the ids it has acked; new subscribers join polling without replay. Polling
    suspends while no
    subscribed session is connected and resumes on reconnect. A merged PR
    auto-unsubscribes everyone and stops polling.

Run manually or via systemd --user (see ./README.md). The relay never spawns it.

Config (env):  NOTIFICATIONS_WS_HOST (default 127.0.0.1)
               NOTIFICATIONS_WS_PORT (default 8137)
               NOTIFICATIONS_DATA_DIR (default ~/.claude/notifications)
               NOTIFICATIONS_TOKEN (default: auto-created <DATA_DIR>/token)
               NOTIFICATIONS_PR_WARM_TTL_SECONDS (default 1800)
               GITHUB_TOKEN, GITHUB_API_URL (default https://api.github.com)
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets", "httpx", "tzdata"]
# ///

import asyncio
import hmac
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import github_client  # noqa: E402
import pr_monitor  # noqa: E402
import pr_schedule  # noqa: E402
import scheduler  # noqa: E402
import wsproto  # noqa: E402

# A scheduled callback this far past due was held while nothing was connected.
RECOVERED_THRESHOLD_SECONDS = 30
# consecutive-no-update level that forces ~max (8h) backoff after an auth failure.
AUTH_BACKOFF_LEVEL = 14
# How long an unsubscribed (subscriber-less, non-terminal) tracker is kept warm
# before the reaper deletes it, so a quick re-subscribe reuses its cached state.
WARM_TTL_DEFAULT_SECONDS = 1800.0


def _warm_ttl_seconds() -> float:
    raw = os.environ.get("NOTIFICATIONS_PR_WARM_TTL_SECONDS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return WARM_TTL_DEFAULT_SECONDS


# session id -> its current connection
CONNECTIONS: dict[str, "Connection"] = {}
# "owner/repo#number" -> PRTracker
TRACKERS: dict[str, pr_monitor.PRTracker] = {}
GH: github_client.GitHubClient | None = None
# Shared secret each relay must present (Authorization: Bearer <token>) to connect.
# Computed once at startup; relays compute the same value from NOTIFICATIONS_DATA_DIR.
TOKEN = ""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# scheduled one-shot callbacks (proof-of-concept feature)
# --------------------------------------------------------------------------- #


def _build_callback_notification(
    entry: dict, session_id: str, now: float
) -> tuple[str, dict[str, str]]:
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


# --------------------------------------------------------------------------- #
# connection handling + delivery
# --------------------------------------------------------------------------- #


class Connection:
    def __init__(self, websocket) -> None:
        self.ws = websocket
        self.session_id: str | None = None
        self.inflight: set[str] = set()  # notification ids sent, awaiting ack
        self.wake = asyncio.Event()  # set to nudge the dispatch loop to deliver now


async def _safe_send(conn: Connection, payload: dict) -> bool:
    try:
        await conn.ws.send(json.dumps(payload))
        return True
    except ConnectionClosed:
        return False


def _wake(session_id: str | None) -> None:
    """Nudge a session's dispatch loop to deliver newly-available notifications.
    Setting an asyncio.Event is sync and safe to call from anywhere in the loop."""
    if not session_id:
        return
    conn = CONNECTIONS.get(session_id)
    if conn is not None:
        conn.wake.set()


def _wake_subscribers(tracker: pr_monitor.PRTracker) -> None:
    """Wake every connected subscriber of a tracker after new events are appended."""
    for sid in tracker.subscribers:
        _wake(sid)


def _next_callback_timeout(session_id: str, now: float) -> float | None:
    """Seconds until this session's soonest not-yet-due scheduled callback, so the
    loop can sleep until it comes due; None if none are pending (wait for a wake)."""
    upcoming = [
        float(e.get("due_at", 0.0)) - now for e in scheduler.pending(session_id)
    ]
    future = [d for d in upcoming if d > 0]
    return min(future) if future else None


async def _dispatch_loop(conn: Connection) -> None:
    """Deliver undelivered notifications (callbacks + PR events) for this session.

    Event-driven: each pass delivers everything currently deliverable, then blocks
    on the wake event until either new content arrives (_wake) or the soonest
    not-yet-due scheduled callback comes due. Idle with nothing pending costs zero CPU."""
    while True:
        session_id = conn.session_id
        timeout: float | None = None
        if session_id:
            now = time.time()
            for entry in scheduler.due_callbacks(session_id, now):
                callback_id = str(entry.get("id", ""))
                if callback_id and callback_id not in conn.inflight:
                    content, meta = _build_callback_notification(entry, session_id, now)
                    if not await _safe_send(
                        conn,
                        {
                            "type": wsproto.NOTIFY,
                            "id": callback_id,
                            "content": content,
                            "meta": meta,
                        },
                    ):
                        return
                    conn.inflight.add(callback_id)
            for tracker in list(TRACKERS.values()):
                if session_id not in tracker.subscribers:
                    continue
                # If events were dropped from the cache while this subscriber was
                # away, surface a one-time "history truncated" notice ahead of the
                # surviving events, so they know to check the PR for what was lost.
                missed = tracker.missed.get(session_id, 0)
                trunc_id = f"trunc:{tracker.key}:{session_id}"
                if missed > 0 and trunc_id not in conn.inflight:
                    content = (
                        f"⚠️ {tracker.key}: {missed} earlier update(s) were dropped "
                        "from the cache before they reached you (the PR was very "
                        "active while this session was away). Check the PR directly "
                        "for anything important."
                    )
                    meta = {
                        "severity": "high",
                        "kind": "pr_truncated",
                        "pr": tracker.key,
                    }
                    if not await _safe_send(
                        conn,
                        {
                            "type": wsproto.NOTIFY,
                            "id": trunc_id,
                            "content": content,
                            "meta": meta,
                        },
                    ):
                        return
                    conn.inflight.add(trunc_id)
                acked = tracker.acked.get(session_id, set())
                for event in tracker.events:
                    event_id = event["id"]
                    if event_id in acked:
                        continue
                    nid = f"pr:{tracker.key}:{event_id}"
                    if nid in conn.inflight:
                        continue
                    payload = {
                        "type": wsproto.NOTIFY,
                        "id": nid,
                        "content": event["content"],
                        "meta": event["meta"],
                    }
                    if not await _safe_send(conn, payload):
                        return
                    conn.inflight.add(nid)
            timeout = _next_callback_timeout(session_id, now)
        try:
            await asyncio.wait_for(conn.wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        conn.wake.clear()


async def _handle(websocket) -> None:
    # Auth gate: validate the shared token once, at connect. The connection is
    # trusted for its lifetime afterwards (we never re-check per message). Use a
    # constant-time compare so a rejected connection's timing can't leak the token.
    provided = websocket.request.headers.get("Authorization") or ""
    if not hmac.compare_digest(provided, f"Bearer {TOKEN}"):
        await websocket.close(code=1008, reason="unauthorized")
        return
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
                new_sid = msg.get("session_id") or None
                if (
                    conn.session_id
                    and CONNECTIONS.get(conn.session_id) is conn
                    and conn.session_id != new_sid
                ):
                    del CONNECTIONS[conn.session_id]
                conn.session_id = new_sid
                if new_sid:
                    CONNECTIONS[new_sid] = conn
                    for tracker in TRACKERS.values():
                        if new_sid in tracker.subscribers:
                            tracker.wake.set()  # resume polling for this session's PRs
                    if dispatch_task is None:
                        dispatch_task = asyncio.create_task(_dispatch_loop(conn))

            elif kind == wsproto.SCHEDULE:
                await _handle_schedule(websocket, conn, msg)

            elif kind == wsproto.ACK:
                _handle_ack(conn, msg)

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

            elif kind == wsproto.SUBSCRIBE_PR:
                await _handle_subscribe(websocket, conn, msg)

            elif kind == wsproto.UNSUBSCRIBE_PR:
                _handle_unsubscribe(conn, msg)
                await _send(websocket, wsproto.UNSUBSCRIBED, msg, pr=_msg_key(msg))

            elif kind == wsproto.LIST_SUBSCRIPTIONS:
                await _handle_list_subscriptions(websocket, conn, msg)
    except ConnectionClosed:
        pass
    finally:
        if dispatch_task is not None:
            dispatch_task.cancel()
        if conn.session_id and CONNECTIONS.get(conn.session_id) is conn:
            del CONNECTIONS[conn.session_id]


async def _handle_schedule(websocket, conn: Connection, msg: dict) -> None:
    session_id = msg.get("session_id") or conn.session_id
    if not session_id:
        await _send(websocket, wsproto.ERROR, msg, error="no session id")
        return
    delay = max(0, int(msg.get("delay_seconds", 300)))
    callback_id = scheduler.schedule(
        session_id, time.time() + delay, kind=str(msg.get("kind", "scheduled"))
    )
    _wake(session_id)  # let the dispatch loop schedule its wake for the new due time
    await _send(
        websocket, wsproto.SCHEDULED, msg, id=callback_id, due_at=time.time() + delay
    )


def _handle_ack(conn: Connection, msg: dict) -> None:
    nid = msg.get("id")
    if not nid:
        return
    if isinstance(nid, str) and nid.startswith("trunc:"):
        # trunc:{key}:{sid} — key holds '/' and '#' but never ':', so rpartition on
        # ':' cleanly splits the trailing session id off the key. Acking the notice
        # clears the missed counter until the next truncation drops more events.
        key, _, session_id = nid[len("trunc:") :].rpartition(":")
        tracker = TRACKERS.get(key)
        if tracker is not None and session_id in tracker.subscribers:
            tracker.missed[session_id] = 0
            pr_monitor.save_subscriber(tracker, session_id)
        conn.inflight.discard(nid)
    elif isinstance(nid, str) and nid.startswith("pr:"):
        key, _, event_id = nid[3:].rpartition(":")
        tracker = TRACKERS.get(key)
        if tracker is not None and conn.session_id and conn.session_id in tracker.acked:
            tracker.acked[conn.session_id].add(event_id)
            conn.inflight.discard(nid)
            pr_monitor.save_subscriber(tracker, conn.session_id)
            _finalize_terminal(tracker, conn.session_id)
    else:
        if conn.session_id:
            scheduler.delete(conn.session_id, nid)
        conn.inflight.discard(nid)


# --------------------------------------------------------------------------- #
# PR subscription handling
# --------------------------------------------------------------------------- #


def _msg_key(msg: dict) -> str:
    return pr_monitor.pr_key(msg.get("owner"), msg.get("repo"), msg.get("number"))


async def _handle_subscribe(websocket, conn: Connection, msg: dict) -> None:
    session_id = conn.session_id or msg.get("session_id")
    owner, repo, number = msg.get("owner"), msg.get("repo"), msg.get("number")
    if not session_id or not owner or not repo or number is None:
        await _send(
            websocket, wsproto.ERROR, msg, error="missing session id or PR reference"
        )
        return
    key = pr_monitor.pr_key(owner, repo, number)
    tracker = TRACKERS.get(key)

    if tracker is None:
        tracker = pr_monitor.PRTracker(owner, repo, int(number), GH)
        try:
            summary = await tracker.initial_poll()
        except Exception as exc:  # noqa: BLE001 - report any fetch failure to the agent
            await _send(
                websocket, wsproto.ERROR, msg, error=f"could not fetch {key}: {exc}"
            )
            return
        TRACKERS[key] = tracker
        tracker.next_poll_at = time.time() + _poll_delay(
            tracker
        )  # baseline done; schedule first real poll
        pr_monitor.save_state(tracker)
        tracker.task = asyncio.create_task(_tracker_loop(tracker))
    else:
        summary = pr_monitor.summarize(tracker.snapshot)

    if tracker.terminal:
        await _send(
            websocket,
            wsproto.SUBSCRIBED,
            msg,
            pr=key,
            summary=summary,
            merged=tracker.merged,
            closed=True,
        )
        return

    tracker.subscribers.add(session_id)
    tracker.idle_since = None  # reactivated; clear any warm marker
    pr_monitor.save_state(
        tracker
    )  # persist so a restart in the gap can't re-idle/reap it
    tracker.acked[session_id] = set(
        tracker.event_ids
    )  # join without replaying old events
    tracker.missed[session_id] = 0  # caught up by definition; no truncation to report
    pr_monitor.save_subscriber(tracker, session_id)
    tracker.wake.set()  # resume polling for this PR
    _wake(session_id)  # deliver any catch-up / future events immediately
    await _send(
        websocket,
        wsproto.SUBSCRIBED,
        msg,
        pr=key,
        summary=summary,
        merged=tracker.merged,
        closed=False,
    )


def _handle_unsubscribe(conn: Connection, msg: dict) -> None:
    session_id = conn.session_id or msg.get("session_id")
    key = _msg_key(msg)
    tracker = TRACKERS.get(key)
    if tracker is not None and session_id in tracker.subscribers:
        tracker.subscribers.discard(session_id)
        tracker.acked.pop(session_id, None)
        tracker.missed.pop(session_id, None)
        pr_monitor.delete_subscriber(tracker, session_id)
        if not tracker.subscribers:
            # Keep a non-terminal tracker warm (polling auto-suspends with no
            # subscribers) so a quick re-subscribe reuses its cached snapshot; the
            # reaper deletes it after the TTL. A terminal tracker, or warm retention
            # disabled, is removed immediately as before.
            if _warm_ttl_seconds() > 0 and not tracker.terminal:
                tracker.idle_since = time.time()
                pr_monitor.save_state(tracker)
            else:
                _remove_tracker(key)


async def _handle_list_subscriptions(websocket, conn: Connection, msg: dict) -> None:
    session_id = conn.session_id or msg.get("session_id")
    items = [
        {
            "pr": key,
            "merged": t.merged,
            "pending": len(t.unacked_for(session_id)),
        }
        for key, t in TRACKERS.items()
        if session_id in t.subscribers
    ]
    await _send(websocket, wsproto.SUBSCRIPTIONS_RESULT, msg, items=items)


def _finalize_terminal(tracker: pr_monitor.PRTracker, session_id: str) -> None:
    """After a subscriber acks the terminal (merged/gone) event, drop them."""
    if (
        tracker.terminal
        and tracker.terminal_id
        and tracker.terminal_id in tracker.acked.get(session_id, set())
    ):
        tracker.subscribers.discard(session_id)
        tracker.acked.pop(session_id, None)
        tracker.missed.pop(session_id, None)
        pr_monitor.delete_subscriber(tracker, session_id)
        if not tracker.subscribers:
            _remove_tracker(tracker.key)


def _poll_delay(tracker: pr_monitor.PRTracker) -> float:
    """Seconds until this tracker's next poll. NOTIFICATIONS_PR_POLL_SECONDS forces
    a fixed cadence (testing / manual override); otherwise use the backoff schedule."""
    override = os.environ.get("NOTIFICATIONS_PR_POLL_SECONDS")
    if override:
        try:
            return max(0.2, float(override))
        except ValueError:
            pass
    nxt = pr_schedule.compute_next_poll(_now_utc(), tracker.consecutive_no_update)
    return max(1.0, (nxt - _now_utc()).total_seconds())


def _remove_tracker(key: str) -> None:
    tracker = TRACKERS.pop(key, None)
    if tracker is not None and tracker.task is not None:
        tracker.task.cancel()
    pr_monitor.delete_tracker(key)


def _reap_idle_trackers(now: float) -> list[str]:
    """Remove warm (subscriber-less, non-terminal) trackers idle past the TTL.
    Self-heals: a subscriber-less non-terminal tracker with no idle marker
    (e.g. loaded from older on-disk state) gets its clock started here.
    Returns the keys removed. No-op when warm retention is disabled."""
    ttl = _warm_ttl_seconds()
    if ttl <= 0:
        return []
    removed: list[str] = []
    for key, tracker in list(TRACKERS.items()):
        if tracker.subscribers or tracker.terminal:
            continue
        if tracker.idle_since is None:
            tracker.idle_since = now
            pr_monitor.save_state(tracker)
            continue
        if now - tracker.idle_since >= ttl:
            _remove_tracker(key)
            removed.append(key)
    return removed


async def _reaper_loop() -> None:
    while True:
        ttl = _warm_ttl_seconds()
        interval = max(1.0, min(ttl / 2.0, 300.0)) if ttl > 0 else 300.0
        await asyncio.sleep(interval)
        _reap_idle_trackers(time.time())


def _emit(tracker: pr_monitor.PRTracker, event: dict) -> None:
    """Record a daemon-originated event (e.g. gone/auth) and append it to the log."""
    pr_monitor.append_events(tracker, tracker.record([event]))
    _wake_subscribers(tracker)  # deliver the gone/auth event without waiting


async def _wait_or_wake(tracker: pr_monitor.PRTracker, seconds: float) -> None:
    try:
        await asyncio.wait_for(tracker.wake.wait(), timeout=max(0.2, seconds))
        tracker.wake.clear()
    except asyncio.TimeoutError:
        pass


async def _tracker_loop(tracker: pr_monitor.PRTracker) -> None:
    """Poll a PR while at least one subscriber is connected; suspend otherwise."""
    while True:
        if not any(s in CONNECTIONS for s in tracker.subscribers):
            tracker.wake.clear()
            await tracker.wake.wait()  # resumed when a subscribed session reconnects
            continue
        # Honor the persisted next-poll time, so a daemon restart or flap doesn't
        # stampede GitHub: each tracker waits out the remainder of its backoff.
        due_in = (tracker.next_poll_at or 0) - time.time()
        if due_in > 0:
            await _wait_or_wake(tracker, due_in)
            continue
        delay: float | None = None
        try:
            added = tracker.record(await tracker.poll_once())
            pr_monitor.append_events(tracker, added)
            if added:
                _wake_subscribers(tracker)  # deliver new PR events immediately
            tracker.consecutive_no_update = (
                0 if added else tracker.consecutive_no_update + 1
            )
        except github_client.GitHubNotFound as exc:
            _emit(
                tracker,
                pr_monitor.synthetic_event(
                    "pr_gone",
                    "high",
                    f"{tracker.key} could not be fetched ({exc} — deleted, or the token lost "
                    "access). Polling stopped and you've been unsubscribed.",
                    tracker.key,
                    f"gone:{tracker.key}",
                ),
            )
            pr_monitor.save_state(tracker)
            return
        except github_client.GitHubRateLimited as exc:
            wait = max(1.0, exc.reset_at - time.time())
            print(
                f"notifications daemon: {tracker.key} rate limited; waiting {int(wait)}s",
                file=sys.stderr,
            )
            delay = wait + random.uniform(1.0, 15.0)  # defer; not a "no update"
        except github_client.GitHubAuthError as exc:
            if not tracker.auth_notified:
                _emit(
                    tracker,
                    pr_monitor.synthetic_event(
                        "pr_auth_error",
                        "high",
                        f"GitHub access to {tracker.key} failed ({exc}). Polling is paused until "
                        "the daemon's GITHUB_TOKEN is fixed (restart the daemon with a valid token).",
                        tracker.key,
                        f"auth_error:{tracker.key}",
                    ),
                )
                tracker.auth_notified = True
            tracker.consecutive_no_update = max(
                tracker.consecutive_no_update, AUTH_BACKOFF_LEVEL
            )
            print(
                f"notifications daemon: auth error {tracker.key}: {exc}",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001 - transient/server/network: back off and retry
            print(
                f"notifications daemon: poll error {tracker.key}: {exc}",
                file=sys.stderr,
            )
            tracker.consecutive_no_update += 1

        if delay is None:
            delay = _poll_delay(tracker)
            throttle_until = GH.should_throttle() if GH is not None else None
            if throttle_until is not None:
                delay = max(
                    delay, throttle_until - time.time() + random.uniform(1.0, 15.0)
                )
        tracker.next_poll_at = time.time() + delay
        pr_monitor.save_state(tracker)
        if tracker.terminal:  # merged: deliver the final event, then stop polling
            return


async def _send(websocket, msg_type: str, request: dict, **fields: object) -> None:
    payload: dict[str, object] = {"type": msg_type, **fields}
    if "req_id" in request:
        payload["req_id"] = request["req_id"]
    try:
        await websocket.send(json.dumps(payload))
    except ConnectionClosed:
        pass


async def main() -> None:
    global GH, TOKEN
    TOKEN = wsproto.token()  # auto-creates <NOTIFICATIONS_DATA_DIR>/token if needed
    GH = github_client.GitHubClient()
    for tracker in pr_monitor.load_trackers(GH):
        TRACKERS[tracker.key] = tracker
        tracker.task = asyncio.create_task(_tracker_loop(tracker))
    asyncio.create_task(_reaper_loop())

    host, port = wsproto.host(), wsproto.port()
    try:
        async with serve(_handle, host, port):
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
