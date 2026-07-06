# vim: filetype=python
"""Detect whether this MCP server was actually loaded as a Claude Code channel.

Nothing over the MCP wire reveals it. But Claude Code writes a per-server MCP log
under its cache and records exactly one of:

    "Channel notifications registered"          -> loaded as a channel (push works)
    "Channel notifications skipped: <reason>"   -> not a channel (pushes are dropped)

The log lives at:
    <cache>/claude-cli-nodejs/<cwd with every '/' and '.' replaced by '-'>/
        mcp-logs-<server>/<timestamp>.jsonl
where <cache> is ~/Library/Caches on macOS, else $XDG_CACHE_HOME or ~/.cache (or
the explicit NOTIFICATIONS_MCP_LOG_CACHE_DIR override, which wins regardless of
platform — used by the tests to make detection platform-independent), and
<server> is the (possibly plugin-namespaced) server name with separators turned
into dashes, e.g. plugin:notifications:notifications ->
mcp-logs-plugin-notifications-notifications.

The relay uses this (after init, with a short retry, since the line is written
around the time the server declares its capability) to decide whether to push to
the channel or fall back to a pull-based catch_up tool. stdlib only.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

REGISTERED = "registered"  # loaded as a channel
SKIPPED = "skipped"  # explicitly not a channel
UNKNOWN = "unknown"  # no log yet / can't tell

_REGISTERED_MARK = "Channel notifications registered"
_SKIPPED_MARK = "Channel notifications skipped"


def _cache_root() -> Path:
    # NOTIFICATIONS_MCP_LOG_CACHE_DIR is an explicit override that wins ahead of the
    # platform branch, so tests (and unusual setups) can point channel detection at a
    # specific cache root regardless of platform — e.g. the e2e suite seeds a fake log
    # under a tmp dir and needs detection to find it even on macOS, where this would
    # otherwise resolve to ~/Library/Caches.
    override = os.environ.get("NOTIFICATIONS_MCP_LOG_CACHE_DIR")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return Path(xdg) if xdg else Path.home() / ".cache"


def encode_cwd(path: str) -> str:
    return re.sub(r"[/.]", "-", path)


def _log_dir(server_name: str, project_dir: str) -> Path | None:
    base = _cache_root() / "claude-cli-nodejs" / encode_cwd(project_dir)
    if not base.is_dir():
        return None
    candidates = [
        p for p in base.glob("mcp-logs-*") if p.is_dir() and server_name in p.name
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def detect_channel_mode(
    server_name: str, project_dir: str | None, *, newer_than: float = 0.0
) -> str:
    """REGISTERED / SKIPPED / UNKNOWN from the latest MCP log written at/after `newer_than`."""
    if not project_dir:
        return UNKNOWN
    directory = _log_dir(server_name, project_dir)
    if directory is None:
        return UNKNOWN
    logs = [p for p in directory.glob("*.jsonl") if p.stat().st_mtime >= newer_than]
    if not logs:
        return UNKNOWN
    latest = max(logs, key=lambda p: p.stat().st_mtime)
    try:
        text = latest.read_text(errors="replace")
    except OSError:
        return UNKNOWN
    registered_at = text.rfind(_REGISTERED_MARK)
    skipped_at = text.rfind(_SKIPPED_MARK)
    if registered_at < 0 and skipped_at < 0:
        return UNKNOWN
    return REGISTERED if registered_at > skipped_at else SKIPPED


def _iso_epoch(ts: object, fallback: float) -> float:
    """Parse a Claude Code log ``timestamp`` (ISO-8601, trailing 'Z') to epoch seconds,
    falling back to the file mtime when it is missing/unparseable, so markers across
    files sort chronologically."""
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def detect_channel_mode_by_session(server_name: str, session_id: str | None) -> str:
    """REGISTERED / SKIPPED / UNKNOWN by matching the marker to THIS session's id across
    every project-keyed mcp-log dir under the cache root.

    Claude Code keys each MCP log dir by the *session* cwd. For a worktree session — or
    any bg agent whose process cwd differs from the session cwd — that dir is not the
    relay's ``os.getcwd()``, so the cwd-scoped probe (``detect_channel_mode``) reads the
    wrong dir and misses the marker (persistent pull-mode misdetection). The marker line
    carries the session id, which is globally unique, so matching on it finds the right
    log regardless of which cwd dir it landed in, and never picks up a *different*
    session's marker (e.g. a claimed-spare sharing the workspace cwd). No freshness gate
    is needed: the id is the disambiguator, and the latest marker for this id wins (so a
    resume that re-registers as a channel, or flips to skipped, is honored)."""
    if not session_id:
        return UNKNOWN
    root = _cache_root() / "claude-cli-nodejs"
    if not root.is_dir():
        return UNKNOWN
    best_key: float | None = None
    best_verdict = UNKNOWN
    for logdir in root.glob("*/mcp-logs-*"):
        if server_name not in logdir.name or not logdir.is_dir():
            continue
        for log in logdir.glob("*.jsonl"):
            try:
                text = log.read_text(errors="replace")
                mtime = log.stat().st_mtime
            except OSError:
                continue
            for line in text.splitlines():
                if session_id not in line:  # cheap prefilter before the JSON parse
                    continue
                if _REGISTERED_MARK in line:
                    verdict = REGISTERED
                elif _SKIPPED_MARK in line:
                    verdict = SKIPPED
                else:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if obj.get("sessionId") != session_id:
                    continue  # id was incidental (substring), not the field value
                key = _iso_epoch(obj.get("timestamp"), mtime)
                if best_key is None or key >= best_key:
                    best_key, best_verdict = key, verdict
    return best_verdict
