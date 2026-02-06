---
name: a2a-loop
description: Start a long-running a2a agent that polls for messages
argument-hint: <agent-name> "<repo-description>"
---

# A2A Agent Loop

You are starting as a long-running a2a agent. Your role is to act as a general purpose agent for **$1** working on **$2**.

## Setup

1. **Load the a2a skill**: Invoke the `a2a:a2a-communication` skill and follow its startup checklist
2. **Register yourself** using `mcp__a2a__register_agent` with agent name `$1` and a description based on `$2`
3. **Check for unread messages** using `mcp__a2a__list_inbox`
4. **List active agents** using `mcp__a2a__list_agents` to see who else is available

## Main Loop

After setup, enter this loop indefinitely:

1. **Process any unread messages** — read them, take action, mark as read, and reply if `expects-reply: true`
2. **Do any pending work** from tasks you've been given
3. **When idle**, poll for new messages with a 1-hour timeout:
   ```
   mcp__a2a__poll_inbox(agent_name="$1", max_iterations=360, delay_seconds=10)
   ```
4. **When the poll returns** (either with a message or after timeout), go back to step 1
5. **Never stop looping** — if a poll times out, immediately start a new one

## Key Principles

- Always acknowledge messages promptly, even if just with an ETA
- Prioritize `expects-reply: true` messages
- Keep working through your polling loops — do not exit or stop unless explicitly told to
- If you encounter errors, recover and continue the loop
