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

import os
import re
import sys
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
