#!/usr/bin/env -S uv run -qs
# vim: filetype=python
"""PreCompact hook for the notifications plugin: a compaction audit breadcrumb.

Fires just before Claude Code compacts the session context. Appends one JSONL
line per compaction recording the trigger (manual vs auto), the session id, a
timestamp, and the transcript's byte size at compaction time. That last number
is the diagnostic one: it shows whether compaction is firing near the intended
threshold or letting context balloon toward the window wall before it acts — the
failure mode behind the late-context wedges this plugin has chased. Fleet
observability only; entirely best-effort, so a compaction never fails because of
us.

Claude Code exposes no PostCompact event, so post-compaction size isn't available
from a hook — the pre-compaction size at each event is the metric we can capture,
and it is the one that answers "where did compaction fire?".

Note on self-compaction (the compact_session tool): it rides Claude Code's AUTO
executor, so its compaction is labeled trigger="auto", NOT "manual" — self-compacts
are indistinguishable from threshold auto-compaction on the trigger field alone.
Distinguish them by a below-threshold transcript_bytes (a self-compact fires well
under the auto threshold) or by the transcript's own compact_boundary tool-result.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import json
import os
import sys
import time
from pathlib import Path


def _audit_path() -> Path:
    """Where the audit log lives. Env override wins (tests point it at a tmp
    file); default is a persistent, PVC-mounted location beside the plugin's
    other client-side state (e.g. agent-identity.json)."""
    override = os.environ.get("NOTIFICATIONS_COMPACTION_AUDIT_FILE")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "notifications-compaction-audit.jsonl"


def _transcript_bytes(payload: dict) -> int | None:
    """Byte size of the session transcript at compaction time — a cheap (a stat,
    no read) proxy for how large the context had grown before compaction fired."""
    path = payload.get("transcript_path")
    if not path:
        return None
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def build_record(payload: dict, now: float) -> dict:
    """The audit line for one compaction event. Pure — the clock is injected so
    the record is testable without patching time."""
    return {
        "event": payload.get("hook_event_name") or "PreCompact",
        "ts": now,
        "session_id": payload.get("session_id"),
        # "manual" (an explicit /compact or the compact_session tool) vs "auto"
        # (Claude Code's threshold-driven compaction). The field is "trigger"; a
        # couple of payload variants spell it "matcher".
        "trigger": payload.get("trigger") or payload.get("matcher"),
        "transcript_bytes": _transcript_bytes(payload),
    }


def append_record(path: Path, record: dict) -> None:
    """Append one JSONL line, creating the parent dir. Append mode keeps the log an
    ordered, greppable audit trail across a session's compactions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, OSError):
        return
    append_record(_audit_path(), build_record(payload, time.time()))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let a hook failure interfere with compaction.
        pass
