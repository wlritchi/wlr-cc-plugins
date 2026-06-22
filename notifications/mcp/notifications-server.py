#!/usr/bin/env -S uv run -qs
# vim: filetype=python
"""
Notifications MCP Server

Groundwork for a Claude Code notification framework. For now this server only
knows how to report which Claude Code session it is attached to.

The session id usually comes from the CLAUDE_CODE_SESSION_ID environment
variable Claude Code hands to the stdio MCP servers it spawns. But when a
session is resumed, that env var holds a stale temporary id. The SessionStart
hook records the real id in a per-instance state file keyed by the Claude Code
process id; this server prefers that file and only falls back to the env var.
See ../lib/session_state.py for the correlation details.

Run directly: ./notifications-server.py
Or via uv:    uv run -qs notifications-server.py
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp"]
# ///

import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import session_state  # noqa: E402

mcp = FastMCP("notifications")


@mcp.tool()
def get_session_id() -> str:
    """Report the Claude Code session ID this notifications server is attached to.

    Prefers the id recorded by the SessionStart hook (correct across `/resume`),
    falling back to the CLAUDE_CODE_SESSION_ID environment variable. Use this to
    confirm the plugin sees the same session you are running in.
    """
    session_id, source = session_state.effective_session_id()
    if not session_id:
        return (
            "No session ID available: neither the SessionStart hook's state file "
            f"nor {session_state.SESSION_ID_ENV_VAR} is set."
        )
    return f"session_id={session_id} (source: {source})"


if __name__ == "__main__":
    mcp.run()
