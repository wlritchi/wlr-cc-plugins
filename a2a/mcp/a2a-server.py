#!/usr/bin/env -S uv run -qs
# vim: filetype=python
"""
A2A (Agent-to-Agent) MCP Server

Provides tools for inter-agent communication via filesystem-based messaging.
Messages are stored in ~/a2a/{agent-name}/ directories as markdown files
with YAML frontmatter.

Run directly: ./a2a-server.py
Or via uv:    uv run -qs a2a-server.py
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp"]
# ///

import re
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

A2A_DIR = Path.home() / "a2a"
AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

mcp = FastMCP("a2a")


def _validate_agent_name(name: str) -> None:
    """Validate agent name format."""
    if not AGENT_NAME_PATTERN.match(name):
        raise ValueError(
            f"Agent name '{name}' is invalid. "
            "Must contain only alphanumeric characters, underscores, or hyphens."
        )


def _get_timestamp() -> str:
    """Get ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_filename_timestamp() -> str:
    """Get filesystem-safe timestamp for filenames."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


@mcp.tool()
def register_agent(
    agent_name: str,
    description: str,
    capabilities: str,
    working_dir: str,
) -> str:
    """Use the a2a:a2a-communication skill first."""
    _validate_agent_name(agent_name)

    # Create directories
    A2A_DIR.mkdir(parents=True, exist_ok=True)
    inbox_dir = A2A_DIR / agent_name
    inbox_dir.mkdir(exist_ok=True)

    agents_file = A2A_DIR / "active-agents.md"
    timestamp = _get_timestamp()

    # Initialize active-agents.md if needed
    if not agents_file.exists():
        agents_file.write_text("# Active Agents\n\n")

    content = agents_file.read_text()

    # Check if agent already registered
    agent_header = f"## {agent_name}"
    changes: list[str] = []

    if agent_header in content:
        # Extract and compare existing values
        lines = content.split("\n")
        in_section = False
        existing_desc = ""
        existing_caps = ""
        existing_dir = ""

        for i, line in enumerate(lines):
            if line == agent_header:
                in_section = True
                # Description is typically 2 lines after header (after blank line)
                if i + 2 < len(lines) and not lines[i + 2].startswith("**"):
                    existing_desc = lines[i + 2]
            elif in_section:
                if line.startswith("## "):
                    break
                if line.startswith("**Capabilities:**"):
                    existing_caps = line.replace("**Capabilities:** ", "")
                elif line.startswith("**Working in:**"):
                    existing_dir = line.replace("**Working in:** ", "")

        if existing_desc != description:
            changes.append(f"description: '{existing_desc}' -> '{description}'")
        if existing_caps != capabilities:
            changes.append(f"capabilities: '{existing_caps}' -> '{capabilities}'")
        if existing_dir != working_dir:
            changes.append(f"working-dir: '{existing_dir}' -> '{working_dir}'")

        # Remove existing entry
        new_lines = []
        skip = False
        for line in lines:
            if line == agent_header:
                skip = True
                continue
            if skip and line.startswith("## "):
                skip = False
            if not skip:
                new_lines.append(line)

        # Clean up extra blank lines at end
        while new_lines and new_lines[-1] == "":
            new_lines.pop()
        content = "\n".join(new_lines)

    # Append new registration
    entry = f"""

## {agent_name}

{description}

**Capabilities:** {capabilities}
**Working in:** {working_dir}
**Started:** {timestamp}
**Status:** active
"""
    content = content.rstrip() + entry
    agents_file.write_text(content)

    if changes:
        changes_str = "\n".join(f"  - {c}" for c in changes)
        return f"Updated registration for '{agent_name}' at {timestamp}\nChanged fields:\n{changes_str}"
    return f"Registered agent '{agent_name}' at {timestamp}"


@mcp.tool()
def unregister_agent(agent_name: str, delete_inbox: bool = False) -> str:
    """Use the a2a:a2a-communication skill first."""
    _validate_agent_name(agent_name)

    agents_file = A2A_DIR / "active-agents.md"

    if not agents_file.exists():
        raise ValueError("No agents file found - nothing to unregister")

    content = agents_file.read_text()
    agent_header = f"## {agent_name}"

    if agent_header not in content:
        raise ValueError(f"Agent '{agent_name}' is not registered")

    # Remove the agent's section
    lines = content.split("\n")
    new_lines = []
    skip = False

    for line in lines:
        if line == agent_header:
            skip = True
            continue
        if skip and line.startswith("## "):
            skip = False
        if not skip:
            new_lines.append(line)

    # Clean up extra blank lines at end
    while new_lines and new_lines[-1] == "":
        new_lines.pop()

    # Clean up multiple consecutive blank lines
    cleaned_lines = []
    prev_blank = False
    for line in new_lines:
        is_blank = line == ""
        if is_blank and prev_blank:
            continue
        cleaned_lines.append(line)
        prev_blank = is_blank

    agents_file.write_text("\n".join(cleaned_lines) + "\n")

    result = f"Unregistered agent '{agent_name}'"

    # Optionally delete inbox
    if delete_inbox:
        inbox_dir = A2A_DIR / agent_name
        if inbox_dir.exists():
            import shutil

            shutil.rmtree(inbox_dir)
            result += " and deleted inbox directory"

    return result


@mcp.tool()
def send_message(
    from_agent: str,
    to_agent: str,
    subject: str,
    expects_reply: bool,
    body: str,
) -> str:
    """Use the a2a:a2a-communication skill first."""
    _validate_agent_name(from_agent)
    _validate_agent_name(to_agent)

    recipient_dir = A2A_DIR / to_agent

    # Warn if recipient doesn't exist but create anyway
    warning = ""
    if not recipient_dir.exists():
        warning = f"Warning: recipient '{to_agent}' may not be registered (inbox doesn't exist)\n"
        recipient_dir.mkdir(parents=True, exist_ok=True)

    timestamp = _get_timestamp()
    filename_timestamp = _get_filename_timestamp()

    # Create subject slug
    subject_slug = subject.lower().replace(" ", "-")
    subject_slug = re.sub(r"[^a-z0-9-]", "", subject_slug)[:50]

    filename = f"{filename_timestamp}-{subject_slug}.md"
    filepath = recipient_dir / filename

    # Write message with YAML frontmatter
    expects_reply_str = "true" if expects_reply else "false"
    message_content = f"""---
from: {from_agent}
to: {to_agent}
timestamp: {timestamp}
subject: {subject}
expects-reply: {expects_reply_str}
---

{body}
"""
    filepath.write_text(message_content)

    return f"{warning}Sent message to {to_agent}: {filepath}"


@mcp.tool()
def poll_inbox(
    agent_name: str,
    max_iterations: int = 30,
    delay_seconds: int = 10,
) -> str:
    """Use the a2a:a2a-communication skill first."""
    _validate_agent_name(agent_name)

    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")

    inbox_dir = A2A_DIR / agent_name

    if not inbox_dir.exists():
        raise ValueError(
            f"Inbox directory not found: {inbox_dir}\nHave you registered this agent?"
        )

    start_time = int(time.time())
    result_lines = [
        f"Polling inbox for {agent_name} (max {max_iterations} iterations, {delay_seconds}s delay)",
        f"Poll started at {start_time}",
    ]

    for i in range(1, max_iterations + 1):
        # Check for unread messages
        for msg_file in sorted(inbox_dir.glob("*.md")):
            seen_file = msg_file.with_suffix(".md.seen")
            if not seen_file.exists():
                content = msg_file.read_text()
                return "\n".join(
                    [
                        *result_lines,
                        f"--- Found unread message (iteration {i}) ---",
                        f"Path: {msg_file}",
                        "--- Content ---",
                        content,
                    ]
                )

        # Sleep unless last iteration
        if i < max_iterations:
            time.sleep(delay_seconds)

    return "\n".join(
        [
            *result_lines,
            f"No unread messages found after {max_iterations} iterations",
        ]
    )


@mcp.tool()
def mark_read(message_path: str) -> str:
    """Use the a2a:a2a-communication skill first."""
    msg_path = Path(message_path).resolve()
    a2a_resolved = A2A_DIR.resolve()

    # Security check: path must be within ~/a2a/
    try:
        msg_path.relative_to(a2a_resolved)
    except ValueError:
        raise ValueError(f"Message path must be within {A2A_DIR}")

    if not msg_path.exists():
        raise ValueError(f"Message file not found: {msg_path}")

    if msg_path.suffix != ".md":
        raise ValueError("Message path must be a .md file")

    seen_path = msg_path.with_suffix(".md.seen")
    seen_path.touch()

    return f"Marked as read: {msg_path}"


@mcp.tool()
def list_agents() -> str:
    """Use the a2a:a2a-communication skill first."""
    agents_file = A2A_DIR / "active-agents.md"

    if not agents_file.exists():
        return "No agents registered yet. Use register_agent to register an agent."

    return agents_file.read_text()


@mcp.tool()
def list_inbox(agent_name: str, include_read: bool = False) -> str:
    """Use the a2a:a2a-communication skill first."""
    _validate_agent_name(agent_name)

    inbox_dir = A2A_DIR / agent_name

    if not inbox_dir.exists():
        raise ValueError(
            f"Inbox directory not found: {inbox_dir}\nHave you registered this agent?"
        )

    messages = sorted(inbox_dir.glob("*.md"))

    if not messages:
        return f"No messages in inbox for {agent_name}"

    lines = [f"Inbox for {agent_name}:", ""]

    for msg_file in messages:
        seen_file = msg_file.with_suffix(".md.seen")
        is_read = seen_file.exists()

        if is_read and not include_read:
            continue

        status = "[read]" if is_read else "[unread]"
        lines.append(f"  {status} {msg_file.name}")

    if len(lines) == 2:
        return f"No {'unread ' if not include_read else ''}messages in inbox for {agent_name}"

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
