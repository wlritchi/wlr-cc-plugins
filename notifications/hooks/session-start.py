#!/usr/bin/env -S uv run -qs
# vim: filetype=python
"""SessionStart hook for the notifications plugin.

Claude Code passes the real session id (as JSON on stdin) here on every
SessionStart, including `resume`. We record it in a per-instance state file so
the long-lived MCP server can pick up the correct id even when its env var holds
the stale temporary id from before a resume. Correlation is by the Claude Code
process both the hook and the server descend from. Entirely best-effort: any
failure is swallowed so a session never fails to start because of us.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import session_state  # noqa: E402


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, OSError):
        return

    session_id = payload.get("session_id")
    if not session_id:
        return

    claude_pid = session_state.resolve_claude_pid()
    if claude_pid is None:
        return  # can't correlate to a server; nothing useful to do

    session_state.write_session(claude_pid, session_id, payload.get("source"))
    session_state.reap_stale()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let a hook failure break session startup.
        pass
