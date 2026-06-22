#!/usr/bin/env -S uv run -qs
# vim: filetype=python
"""
Notifications MCP Server

Groundwork for a Claude Code notification framework. For now this server only
knows how to report which Claude Code session it is attached to, by reading the
CLAUDE_CODE_SESSION_ID environment variable that Claude Code exposes to the
stdio MCP servers it spawns.

The single tool exists so the agent can confirm the plugin's understanding of
the current session ID before later capabilities are built on top of it.

Run directly: ./notifications-server.py
Or via uv:    uv run -qs notifications-server.py
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp"]
# ///

import os

from mcp.server.fastmcp import FastMCP

SESSION_ID_ENV_VAR = "CLAUDE_CODE_SESSION_ID"

mcp = FastMCP("notifications")


@mcp.tool()
def get_session_id() -> str:
    """Report the Claude Code session ID this notifications server is attached to.

    Reads the CLAUDE_CODE_SESSION_ID environment variable from the server's own
    environment (inherited from the Claude Code process that spawned it). Use
    this to confirm the plugin sees the same session you are running in.
    """
    session_id = os.environ.get(SESSION_ID_ENV_VAR)
    if not session_id:
        return (
            f"No session ID available: {SESSION_ID_ENV_VAR} is not set in the "
            "notifications server's environment."
        )
    return f"{SESSION_ID_ENV_VAR}={session_id}"


if __name__ == "__main__":
    mcp.run()
