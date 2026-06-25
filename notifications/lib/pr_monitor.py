# vim: filetype=python
"""GitHub PR monitoring: snapshot, diff -> events, notification formatting, and
a per-PR tracker with on-disk persistence.

The pure functions (snapshot_from_api, diff, summarize and the formatters) carry
the "enough information to act without opening GitHub" requirement and are
unit-tested directly. PRTracker holds the mutable per-PR state (subscribers,
high-water marks, cached events, last snapshot); the daemon drives its polling.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

SHORT_RANGE_LINES = 10  # inline comments at/under this many lines include the code
SHORT_BODY_CHARS = 600  # comment/review bodies at/under this are included in full
MAX_CACHED_EVENTS = 200

_REVIEW_LABEL = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "requested changes",
    "COMMENTED": "left review comments",
    "DISMISSED": "had a review dismissed",
}
_FAILED_CONCLUSIONS = {
    "failure",
    "timed_out",
    "action_required",
    "cancelled",
    "startup_failure",
}
_PR_REF_RE = re.compile(r"^\s*([^/\s]+)/([^/#\s]+)#(\d+)\s*$")


def pr_key(owner: str, repo: str, number: int | str) -> str:
    return f"{owner}/{repo}#{number}"


def parse_pr_ref(ref: str) -> tuple[str, str, int] | None:
    m = _PR_REF_RE.match(ref or "")
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


# --------------------------------------------------------------------------- #
# snapshot + diff (pure)
# --------------------------------------------------------------------------- #


def snapshot_from_api(data: dict) -> dict:
    """Normalize a raw GitHub fetch into the comparable state we diff on."""
    pr = data.get("pr") or {}
    snap: dict = {
        "state": pr.get("state"),
        "merged": bool(pr.get("merged")),
        "merged_by": (pr.get("merged_by") or {}).get("login"),
        "mergeable_state": pr.get("mergeable_state"),
        "head_sha": (pr.get("head") or {}).get("sha"),
        "title": pr.get("title"),
        "url": pr.get("html_url"),
        "reviews": {},
        "review_comments": {},
        "issue_comments": {},
        "check_runs": {},
        "statuses": {},
    }
    for r in data.get("reviews") or []:
        if r.get("state") == "PENDING":
            continue
        snap["reviews"][str(r.get("id"))] = {
            "state": r.get("state"),
            "user": (r.get("user") or {}).get("login"),
            "body": r.get("body") or "",
            "url": r.get("html_url"),
        }
    for c in data.get("review_comments") or []:
        snap["review_comments"][str(c.get("id"))] = {
            "user": (c.get("user") or {}).get("login"),
            "body": c.get("body") or "",
            "path": c.get("path"),
            "line": c.get("line"),
            "start_line": c.get("start_line"),
            "original_line": c.get("original_line"),
            "original_start_line": c.get("original_start_line"),
            "diff_hunk": c.get("diff_hunk"),
            "url": c.get("html_url"),
        }
    for c in data.get("issue_comments") or []:
        snap["issue_comments"][str(c.get("id"))] = {
            "user": (c.get("user") or {}).get("login"),
            "body": c.get("body") or "",
            "url": c.get("html_url"),
        }
    for ch in data.get("check_runs") or []:
        out = ch.get("output") or {}
        snap["check_runs"][str(ch.get("id"))] = {
            "name": ch.get("name"),
            "status": ch.get("status"),
            "conclusion": ch.get("conclusion"),
            "url": ch.get("html_url"),
            "title": out.get("title"),
            "summary": out.get("summary"),
        }
    for s in (data.get("status") or {}).get("statuses") or []:
        snap["statuses"][s.get("context")] = {
            "state": s.get("state"),
            "url": s.get("target_url"),
            "desc": s.get("description"),
        }
    return snap


def _nodes(connection: dict | None) -> list[dict]:
    return (connection or {}).get("nodes") or []


def _mergeable_map(value: str | None) -> str:
    return {"MERGEABLE": "clean", "CONFLICTING": "dirty"}.get(value or "", "unknown")


def _reviewer_name(node: dict | None) -> str | None:
    if not node:
        return None
    return node.get("login") or node.get("name")


def snapshot_from_graphql(pr: dict) -> dict:
    """Normalize a GraphQL pullRequest node into the same snapshot shape as REST.

    Enums are lowercased and CONFLICTING -> "dirty" so the transport-agnostic
    diff() is unchanged. Extra facet fields (draft, labels, requested reviewers,
    review-thread resolution, force pushes) are stashed for later diff rules.
    """
    snap: dict = {
        "state": "open" if pr.get("state") == "OPEN" else "closed",
        "merged": bool(pr.get("merged")),
        "merged_by": (pr.get("mergedBy") or {}).get("login"),
        "mergeable_state": _mergeable_map(pr.get("mergeable")),
        "head_sha": pr.get("headRefOid"),
        "title": pr.get("title"),
        "url": pr.get("url"),
        "draft": bool(pr.get("isDraft")),
        "labels": sorted(
            str(n["name"]) for n in _nodes(pr.get("labels")) if n.get("name")
        ),
        "requested_reviewers": sorted(
            r
            for n in _nodes(pr.get("reviewRequests"))
            if (r := _reviewer_name(n.get("requestedReviewer")))
        ),
        "reviews": {},
        "review_comments": {},
        "issue_comments": {},
        "check_runs": {},
        "statuses": {},
        "review_threads": {},
        "force_pushes": [],
    }
    for r in _nodes(pr.get("reviews")):
        if r.get("state") == "PENDING":
            continue
        snap["reviews"][str(r.get("id"))] = {
            "state": r.get("state"),
            "user": (r.get("author") or {}).get("login"),
            "body": r.get("body") or "",
            "url": r.get("url"),
        }
    for th in _nodes(pr.get("reviewThreads")):
        snap["review_threads"][str(th.get("id"))] = {
            "resolved": bool(th.get("isResolved")),
            "path": th.get("path"),
            "line": th.get("line"),
            "start_line": th.get("startLine"),
        }
        for c in _nodes(th.get("comments")):
            snap["review_comments"][str(c.get("id"))] = {
                "user": (c.get("author") or {}).get("login"),
                "body": c.get("body") or "",
                "path": c.get("path") or th.get("path"),
                "line": c.get("line") if c.get("line") is not None else th.get("line"),
                "start_line": c.get("startLine")
                if c.get("startLine") is not None
                else th.get("startLine"),
                "original_line": c.get("originalLine"),
                "original_start_line": c.get("originalStartLine"),
                "diff_hunk": c.get("diffHunk"),
                "url": c.get("url"),
            }
    for c in _nodes(pr.get("comments")):
        snap["issue_comments"][str(c.get("id"))] = {
            "user": (c.get("author") or {}).get("login"),
            "body": c.get("body") or "",
            "url": c.get("url"),
        }
    commits = _nodes(pr.get("commits"))
    rollup = (
        (commits[0].get("commit") or {}).get("statusCheckRollup") if commits else None
    )
    for ctx in _nodes((rollup or {}).get("contexts")):
        if ctx.get("__typename") == "CheckRun":
            snap["check_runs"][str(ctx.get("id"))] = {
                "name": ctx.get("name"),
                "status": (ctx.get("status") or "").lower(),
                "conclusion": (ctx.get("conclusion") or "").lower() or None,
                "url": ctx.get("detailsUrl"),
                "title": ctx.get("title"),
                "summary": ctx.get("summary"),
            }
        elif ctx.get("__typename") == "StatusContext":
            snap["statuses"][ctx.get("context")] = {
                "state": (ctx.get("state") or "").lower(),
                "url": ctx.get("targetUrl"),
                "desc": ctx.get("description"),
            }
    for ev in _nodes(pr.get("timelineItems")):
        if ev.get("__typename") == "HeadRefForcePushedEvent":
            snap["force_pushes"].append(
                {
                    "before": (ev.get("beforeCommit") or {}).get("oid"),
                    "after": (ev.get("afterCommit") or {}).get("oid"),
                }
            )
    return snap


def diff(old: dict | None, new: dict, key: str) -> list[dict]:
    """Events introduced by `new` relative to `old`. None old => baseline (no events)."""
    if old is None:
        return []
    if new["merged"] and not old["merged"]:
        return [_merged_event(new, key)]  # terminal; nothing else matters

    events: list[dict] = []
    if old["state"] == "open" and new["state"] == "closed" and not new["merged"]:
        events.append(
            _event(
                "pr_closed",
                "info",
                f"{key} was closed without merging.\n{new['url']}",
                key,
                new["url"],
            )
        )
    if new["mergeable_state"] == "dirty" and old["mergeable_state"] != "dirty":
        events.append(
            _event(
                "pr_conflict",
                "high",
                f"{key} now has merge conflicts and can't be merged cleanly; "
                f"rebase/merge the base branch and resolve them.\n{new['url']}",
                key,
                new["url"],
            )
        )
    if old["head_sha"] and new["head_sha"] and old["head_sha"] != new["head_sha"]:
        events.append(
            _event(
                "pr_commits",
                "info",
                f"New commits pushed to {key} (head is now {new['head_sha'][:7]}).\n{new['url']}",
                key,
                new["url"],
            )
        )
    for rid, r in new["reviews"].items():
        if rid not in old["reviews"]:
            events.append(_review_event(r, key))
    for cid, c in new["review_comments"].items():
        if cid not in old["review_comments"]:
            events.append(_inline_comment_event(c, key))
    for cid, c in new["issue_comments"].items():
        if cid not in old["issue_comments"]:
            events.append(_comment_event(c, key))
    for chid, ch in new["check_runs"].items():
        prev = old["check_runs"].get(chid)
        became_complete = ch["status"] == "completed" and (
            prev is None or prev.get("status") != "completed"
        )
        conclusion_changed = (
            prev is not None
            and ch["status"] == "completed"
            and prev.get("conclusion") != ch.get("conclusion")
        )
        if became_complete or conclusion_changed:
            events.append(_check_event(ch, key))
    for ctx, s in new["statuses"].items():
        prev = old["statuses"].get(ctx)
        if s["state"] in ("success", "failure", "error") and (
            prev is None or prev.get("state") != s["state"]
        ):
            events.append(_status_event(ctx, s, key))
    return events


def summarize(snap: dict | None) -> str:
    """One-line current-status summary for the subscribe confirmation."""
    if not snap:
        return "unknown"
    checks = snap["check_runs"].values()
    passed = sum(1 for c in checks if c.get("conclusion") == "success")
    failed = sum(1 for c in checks if c.get("conclusion") in _FAILED_CONCLUSIONS)
    pending = sum(1 for c in checks if c.get("status") != "completed")
    parts = [snap.get("state") or "?"]
    if snap.get("merged"):
        parts.append("merged")
    if snap.get("mergeable_state"):
        parts.append(f"mergeable={snap['mergeable_state']}")
    parts.append(f"checks {passed} pass / {failed} fail / {pending} pending")
    parts.append(f"{len(snap['reviews'])} reviews")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# formatting (pure)
# --------------------------------------------------------------------------- #


def _short(text: str | None, limit: int = SHORT_BODY_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "… (truncated — see link)"


def _hunk_tail(hunk: str | None, n: int) -> str:
    if not hunk:
        return ""
    body = [ln for ln in hunk.splitlines() if not ln.startswith("@@")]
    tail = body[-max(1, n) :]
    return "\n".join(ln[1:] if ln[:1] in " +-" else ln for ln in tail)


def _event(kind: str, severity: str, content: str, key: str, url: str | None) -> dict:
    meta = {"severity": severity, "kind": kind, "pr": key}
    if url:
        meta["url"] = url
    return {"type": kind, "content": content, "meta": meta, "created_at": time.time()}


def synthetic_event(
    kind: str, severity: str, content: str, key: str, url: str | None = None
) -> dict:
    """Event the daemon emits itself (e.g. a fetch failure), not from a diff."""
    return _event(kind, severity, content, key, url)


def _merged_event(new: dict, key: str) -> dict:
    who = new.get("merged_by") or "someone"
    content = (
        f"{key} was merged by {who}. All subscribers have been unsubscribed and "
        f"polling has stopped.\n{new['url']}"
    )
    return _event("pr_merged", "high", content, key, new["url"])


def _review_event(r: dict, key: str) -> dict:
    label = _REVIEW_LABEL.get(r["state"], (r["state"] or "reviewed").lower())
    severity = "high" if r["state"] == "CHANGES_REQUESTED" else "info"
    body = _short(r.get("body"))
    body_part = f"\n{body}" if body else ""
    content = f"Review on {key}: {r['user']} {label}.{body_part}\n{r['url']}"
    return _event("pr_review", severity, content, key, r["url"])


def _comment_event(c: dict, key: str) -> dict:
    content = f"New comment on {key} by {c['user']}:\n{_short(c['body'])}\n{c['url']}"
    return _event("pr_comment", "info", content, key, c["url"])


def _inline_comment_event(c: dict, key: str) -> dict:
    path = c.get("path")
    end = c.get("line") or c.get("original_line")
    start = c.get("start_line") or c.get("original_start_line") or end
    if start and end and start > end:
        start, end = end, start
    if start and end and start != end:
        loc = f"{path}:{start}-{end}"
        span = end - start + 1
    else:
        loc = f"{path}:{end}"
        span = 1
    if span <= SHORT_RANGE_LINES:
        code = _hunk_tail(c.get("diff_hunk"), span)
        snippet = f"\n```\n{code}\n```" if code else ""
    else:
        snippet = f"\n(comment spans lines {start}–{end})"
    content = (
        f"Inline comment on {key} by {c.get('user')} at {loc}:{snippet}\n"
        f"> {_short(c.get('body'))}\n{c.get('url')}"
    )
    return _event("pr_inline_comment", "info", content, key, c.get("url"))


def _check_event(ch: dict, key: str) -> dict:
    outcome = ch.get("conclusion") or ch.get("status")
    severity = "high" if ch.get("conclusion") in _FAILED_CONCLUSIONS else "info"
    parts = [f"Check '{ch.get('name')}' on {key}: {outcome}."]
    if ch.get("title"):
        parts.append(_short(ch.get("title"), 200))
    if severity == "high" and ch.get("summary"):
        parts.append(_short(ch.get("summary"), 600))
    if ch.get("url"):
        parts.append(ch["url"])
    return _event("pr_check", severity, "\n".join(parts), key, ch.get("url"))


def _status_event(ctx: str, s: dict, key: str) -> dict:
    severity = "high" if s["state"] in ("failure", "error") else "info"
    parts = [f"Status '{ctx}' on {key}: {s['state']}."]
    if s.get("desc"):
        parts.append(_short(s.get("desc"), 200))
    if s.get("url"):
        parts.append(s["url"])
    return _event("pr_status", severity, "\n".join(parts), key, s.get("url"))


# --------------------------------------------------------------------------- #
# tracker + persistence
# --------------------------------------------------------------------------- #


class PRTracker:
    """Mutable per-PR state. The daemon owns the polling/delivery around it."""

    def __init__(self, owner: str, repo: str, number: int, client) -> None:
        self.owner = owner
        self.repo = repo
        self.number = number
        self.key = pr_key(owner, repo, number)
        self.client = client
        self.subscribers: set[str] = set()
        self.hwm: dict[str, int] = {}
        self.events: list[dict] = []
        self.next_seq = 1
        self.snapshot: dict | None = None
        self.consecutive_no_update = 0
        self.merged = False
        self.terminal = False  # merged or gone: stop polling, unsubscribe on ack
        self.terminal_seq: int | None = None
        self.auth_notified = False  # the one-time auth-failure event was emitted
        self.task = None
        self.wake = asyncio.Event()

    def max_seq(self) -> int:
        return self.next_seq - 1

    def events_after(self, seq: int) -> list[dict]:
        return [e for e in self.events if e["seq"] > seq]

    def append(self, events: list[dict]) -> None:
        for e in events:
            e["seq"] = self.next_seq
            self.next_seq += 1
            self.events.append(e)
            if e["type"] in ("pr_merged", "pr_gone"):
                self.terminal = True
                self.terminal_seq = e["seq"]
        if len(self.events) > MAX_CACHED_EVENTS:
            self.events = self.events[-MAX_CACHED_EVENTS:]

    async def initial_poll(self) -> str:
        """Establish the baseline snapshot (no events) and return a status summary."""
        pr = await self.client.fetch_pr(self.owner, self.repo, self.number)
        self.snapshot = snapshot_from_graphql(pr)
        self.merged = self.snapshot["merged"]
        return summarize(self.snapshot)

    async def poll_once(self) -> bool:
        pr = await self.client.fetch_pr(self.owner, self.repo, self.number)
        new = snapshot_from_graphql(pr)
        events = diff(self.snapshot, new, self.key)
        self.snapshot = new
        if events:
            self.append(events)
            self.consecutive_no_update = 0
        else:
            self.consecutive_no_update += 1
        if new["merged"]:
            self.merged = True
        return bool(events)


def pr_store_dir() -> Path:
    base = os.environ.get("NOTIFICATIONS_DATA_DIR")
    root = Path(base) if base else Path.home() / ".claude" / "notifications"
    return root / "pr"


def _store_path(key: str) -> Path:
    return pr_store_dir() / f"{re.sub(r'[^A-Za-z0-9_.#-]', '_', key)}.json"


def save_tracker(t: PRTracker) -> None:
    directory = pr_store_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "owner": t.owner,
        "repo": t.repo,
        "number": t.number,
        "subscribers": sorted(t.subscribers),
        "hwm": t.hwm,
        "next_seq": t.next_seq,
        "events": t.events[-MAX_CACHED_EVENTS:],
        "snapshot": t.snapshot,
        "consecutive_no_update": t.consecutive_no_update,
        "merged": t.merged,
        "terminal": t.terminal,
        "terminal_seq": t.terminal_seq,
        "auth_notified": t.auth_notified,
    }
    path = _store_path(t.key)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def delete_tracker(key: str) -> None:
    try:
        _store_path(key).unlink()
    except OSError:
        pass


def load_trackers(client) -> list[PRTracker]:
    directory = pr_store_dir()
    if not directory.is_dir():
        return []
    trackers: list[PRTracker] = []
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        t = PRTracker(data["owner"], data["repo"], int(data["number"]), client)
        t.subscribers = set(data.get("subscribers", []))
        t.hwm = {k: int(v) for k, v in (data.get("hwm") or {}).items()}
        t.next_seq = int(data.get("next_seq", 1))
        t.events = data.get("events", [])
        t.snapshot = data.get("snapshot")
        t.consecutive_no_update = int(data.get("consecutive_no_update", 0))
        t.merged = bool(data.get("merged"))
        t.terminal = bool(data.get("terminal"))
        t.terminal_seq = data.get("terminal_seq")
        t.auth_notified = bool(data.get("auth_notified"))
        trackers.append(t)
    return trackers
