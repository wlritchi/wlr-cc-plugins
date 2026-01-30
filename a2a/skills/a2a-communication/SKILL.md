---
name: a2a-communication
description: Use when working on multi-repo projects, collaborating with other agents, running as a long-lived worker agent, or needing to notify another agent of changes
allowed-tools:
  - mcp__plugin_a2a_a2a__register_agent
  - mcp__plugin_a2a_a2a__unregister_agent
  - mcp__plugin_a2a_a2a__send_message
  - mcp__plugin_a2a_a2a__poll_inbox
  - mcp__plugin_a2a_a2a__mark_read
  - mcp__plugin_a2a_a2a__list_agents
  - mcp__plugin_a2a_a2a__list_inbox
---

# Agent-to-Agent Communication

This skill enables communication between Claude Code agents running in separate sessions (e.g., different tmux panes, different repos). Use it when:

- Working on a multi-repo project where changes in one repo affect another
- Running as a specialist agent (devspace manager, CI watcher, etc.)
- Collaborating on a large project with decomposed tasks
- You need to notify another agent of changes or request their help

## MCP Tools

This plugin provides MCP tools for a2a operations. The tools are available as `mcp__a2a__<tool_name>`.

### Available Tools

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `register_agent` | Register yourself as an agent | `agent_name`, `description`, `capabilities`, `working_dir` |
| `unregister_agent` | Unregister an agent | `agent_name`, `delete_inbox` (optional) |
| `send_message` | Send a message to another agent | `from_agent`, `to_agent`, `subject`, `expects_reply`, `body` |
| `mark_read` | Mark a message as read | `message_path` |
| `poll_inbox` | Poll for new messages | `agent_name`, `max_iterations`, `delay_seconds` |
| `list_agents` | List all registered agents | (none) |
| `list_inbox` | List messages in an inbox | `agent_name`, `include_read` (optional) |

## Directory Structure

```
~/a2a/
├── active-agents.md              # Registry of all agents
├── {agent-name}/                 # Inbox for each agent
│   ├── {timestamp}-{subject}.md  # Messages
│   └── {timestamp}-{subject}.md.seen  # Read markers (0-byte)
```

---

## Critical: Agent Lifecycle and Message Handling

**When expecting to receive a2a messages, DO NOT send a final response to the user who launched you.**

In SDK-style interactions (`claude -p "Prompt here..."`), returning a final message to the user terminates the agent. If you're running as a long-lived agent waiting for messages:

- **DO NOT** announce to the user "I'm ready and listening for messages" - this terminates you
- **DO NOT** send status updates that require no user action
- **DO** enter your polling/watching loop silently
- **DO** only communicate with other agents via the a2a directory
- **DO** use background processes for watching if the harness supports it

If you need to report status to the user, ensure you can continue your polling loop afterward. In most SDK contexts, any response you generate ends your session.

---

## On Startup: Register Yourself

When starting as an agent that may communicate with others:

### 1. Choose your agent name

Use kebab-case, descriptive of your role:
- `devspace-manager`
- `backend-api`
- `frontend-app`
- `pa` (personal assistant)

### 2. Register using the MCP tool

```
mcp__a2a__register_agent(
    agent_name="devspace-manager",
    description="Manages the devspace development environment",
    capabilities="devspace operations, k8s/docker, environment troubleshooting",
    working_dir="~/repos/infrastructure"
)
```

This creates your inbox directory and adds your entry to `~/a2a/active-agents.md`.

---

## Sending Messages

### Using the MCP tool

```
mcp__a2a__send_message(
    from_agent="devspace-manager",
    to_agent="backend-api",
    subject="Database connection ready",
    expects_reply=false,
    body="The devspace DB is now available at postgres://dev:dev@localhost:5432/app.\n\nConnection verified and migrations applied."
)
```

### Message format (for reference)

Messages are stored as markdown files with YAML frontmatter:

```markdown
---
from: {your-agent-name}
to: {recipient-agent-name}
timestamp: {ISO 8601}
subject: {brief subject line}
expects-reply: {true|false}
---

{Message body - be clear and include necessary context}
```

### Filename convention

`{timestamp}-{subject-slug}.md`

- **Filename timestamp:** ISO 8601 with hyphens replacing colons for filesystem safety (e.g., `2026-01-16T10-30-00Z`)
- **YAML frontmatter timestamp:** Standard ISO 8601 with colons (e.g., `2026-01-16T10:30:00Z`)
- Subject slug: lowercase, hyphens, brief (e.g., `db-ready`, `schema-change`)

---

## Checking Your Inbox

### List inbox contents

```
mcp__a2a__list_inbox(agent_name="my-agent")
```

To include already-read messages:

```
mcp__a2a__list_inbox(agent_name="my-agent", include_read=true)
```

### Poll for messages

```
mcp__a2a__poll_inbox(
    agent_name="my-agent",
    max_iterations=30,
    delay_seconds=10
)
```

This polls 30 times with 10-second delays (5 minutes total). It returns the first unread message found, or indicates no messages were found after all iterations.

### Mark a message as read

After processing a message, mark it as read:

```
mcp__a2a__mark_read(message_path="/home/user/a2a/my-agent/2026-01-16T10-30-00Z-db-ready.md")
```

### Process messages workflow

When you find an unread message:

1. The poll tool returns the message path and content
2. Determine if action is needed
3. If `expects-reply: true`, prioritize responding
4. Mark as read using `mark_read`

---

## Watching for New Messages

When blocked or idle, watch your inbox for new messages.

### Recommended: Use poll_inbox

```
mcp__a2a__poll_inbox(
    agent_name="my-agent",
    max_iterations=360,
    delay_seconds=10
)
```

This polls for 1 hour (360 iterations × 10 seconds). The tool prints the start timestamp to help with timeout discovery.

### Timeout Handling

When running long polling operations:

- Default Claude Code timeout may be 60 seconds unless configured otherwise
- When a command times out, the tool may be backgrounded
- The polling tool prints a start timestamp - compare it to current time to learn your actual timeout

**Passive timeout discovery:**

If your polling gets backgrounded mid-execution:

1. Note current time vs. the printed start timestamp to learn actual timeout
2. Start a fresh poll with fewer iterations sized to stay under the learned timeout

---

## The Agent Loop

When running as a long-lived agent:

```
┌─────────────────────────────────────────────────────────┐
│  1. Do primary work (if any pending tasks)              │
│  2. Poll inbox for unread messages                      │
│  3. Process any unread messages (mark read after)       │
│  4. If blocked (waiting for reply / no work):           │
│     └─> Poll inbox with long timeout                    │
│  5. On new message or timeout, goto 1                   │
└─────────────────────────────────────────────────────────┘
```

---

## Communication Protocols

These are soft guidelines, not enforced by infrastructure:

### Responsiveness

- When busy, acknowledge receipt: "Got your message, will address after I finish X"
- If `expects-reply: true`, prioritize responding even if just with an ETA
- Periodically check inbox even during focused work (every 15-30 min)

### Follow-up etiquette

- No reply after ~30 minutes to `expects-reply` message? Send a polite follow-up
- After 2-3 follow-ups with no response, escalate to human
- Include context: "Following up on my earlier message about X"

### Escalation

- If an agent seems unresponsive, ask the human: "I've messaged devspace-manager twice with no response - could you check if it's running?"
- Consider messaging a supervisor or PA agent if one exists

### Shutdown courtesy

- Before going inactive, reply to pending `expects-reply` messages
- Unregister yourself: `mcp__a2a__unregister_agent(agent_name="my-agent")`
- Optionally notify collaborators: "Shutting down, will resume tomorrow"

---

## When to Use A2A Proactively

**Multi-repo work:**
- Changing an API schema? Message agents in repos that consume it
- Deploying a new service version? Notify dependent agents

**Blocking dependencies:**
- Need a database provisioned? Message the devspace agent
- Waiting on a code review? Message the reviewer agent

**Coordination:**
- Completed a task another agent was waiting on? Let them know
- Starting/stopping significant work? Announce to collaborators

**Breaking changes:**
- Any change that could break other agents' work deserves a heads-up

---

## Message Examples

See [message-examples.md](message-examples.md) for full examples of:
- Request with expected reply
- Notification (no reply needed)
- Acknowledgment
- Follow-up messages

---

## Startup Checklist

When beginning a session where you'll use A2A:

- [ ] Choose your agent name (kebab-case)
- [ ] Register using `mcp__a2a__register_agent`
- [ ] Check for unread messages using `mcp__a2a__list_inbox` or `mcp__a2a__poll_inbox`
- [ ] List active agents using `mcp__a2a__list_agents`

---

## Related Commands

- **/a2a-loop** - Start a long-running a2a agent with Ralph loop for autonomous operation
