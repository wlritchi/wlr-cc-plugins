---
name: a2a-communication
description: Use when working on multi-repo projects, collaborating with other agents, running as a long-lived worker agent, or needing to notify another agent of changes
---

# Agent-to-Agent Communication

This skill enables communication between Claude Code agents running in separate sessions (e.g., different tmux panes, different repos). Use it when:

- Working on a multi-repo project where changes in one repo affect another
- Running as a specialist agent (devspace manager, CI watcher, etc.)
- Collaborating on a large project with decomposed tasks
- You need to notify another agent of changes or request their help

## Helper Scripts

This skill includes helper scripts for common operations. These scripts make it easier to whitelist a2a operations in Claude Code's approval system.

**Locating the scripts:** Use a two-step pattern:

1. **First Bash call** - find all skill directories (there may be multiple versions):
   ```bash
   find ~/.claude/plugins/cache -type d -name a2a-communication
   ```
   This returns paths like:
   ```
   ~/.claude/plugins/cache/wlr-cc-plugins/a2a/0.2.0/skills/a2a-communication
   ~/.claude/plugins/cache/wlr-cc-plugins/a2a/0.3.0/skills/a2a-communication
   ```
   **Pick the one with the highest version number.**

2. **Second Bash call** - run the script using the path from step 1 (keep the `~` prefix):
   ```bash
   ~/.claude/plugins/cache/wlr-cc-plugins/a2a/0.3.0/skills/a2a-communication/scripts/register-agent.sh arg1 arg2 ...
   ```

**Important:** Run `find` as a separate Bash call first, then use the returned path directly in subsequent script calls. Only run one script per Bash call. Do NOT combine `find` with script execution in a single command.

### Available Scripts

| Script | Purpose | Arguments |
|--------|---------|-----------|
| `register-agent.sh` | Register yourself as an agent | `<name> <description> <capabilities> <working-dir>` |
| `send-message.sh` | Send a message to another agent | `<from> <to> <subject> <expects-reply> [body]` |
| `mark-read.sh` | Mark a message as read | `<message-path>` |
| `poll-inbox.sh` | Poll for new messages | `<agent-name> <max-iterations> <delay-seconds>` |

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

### 2. Register using the helper script

First, find the skill directory:
```bash
find ~/.claude/plugins/cache -type d -name a2a-communication -print -quit
```

Then run the registration script with the returned path:
```bash
~/.claude/plugins/cache/.../a2a-communication/scripts/register-agent.sh devspace-manager "Manages the devspace development environment" "devspace operations, k8s/docker, environment troubleshooting" ~/repos/infrastructure
```

This creates your inbox directory and adds your entry to `~/a2a/active-agents.md`.

---

## Sending Messages

### Using the helper script

After finding the skill directory (see above), run the send script:

```bash
~/.claude/plugins/cache/.../a2a-communication/scripts/send-message.sh devspace-manager backend-api "Database connection ready" false "The devspace DB is now available at postgres://dev:dev@localhost:5432/app. Connection verified and migrations applied."
```

For longer messages, pipe the body via stdin:

```bash
echo "The devspace DB is now available at postgres://dev:dev@localhost:5432/app.

Connection verified and migrations applied. You can proceed with API integration." | ~/.claude/plugins/cache/.../a2a-communication/scripts/send-message.sh devspace-manager backend-api "Database connection ready" false
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

### Poll for messages using the helper script

After finding the skill directory, run the poll script:

```bash
~/.claude/plugins/cache/.../a2a-communication/scripts/poll-inbox.sh my-agent 30 10
```

This polls 30 times with 10-second delays (5 minutes total). It outputs the first unread message found and exits with code 0 if found, code 1 if nothing after all iterations.

### Mark a message as read

After processing a message, mark it as read:

```bash
~/.claude/plugins/cache/.../a2a-communication/scripts/mark-read.sh ~/a2a/my-agent/2026-01-16T10-30-00Z-db-ready.md
```

### Process messages workflow

When you find an unread message:

1. The poll script outputs the message path and content
2. Determine if action is needed
3. If `expects-reply: true`, prioritize responding
4. Mark as read using the mark-read script

---

## Watching for New Messages

When blocked or idle, watch your inbox for new messages.

### Environment Configuration

For long-running agents, extend the bash timeout in `~/.claude/settings.json`:

```json
{
  "BASH_DEFAULT_TIMEOUT_MS": 3600000
}
```

This allows foreground polling for up to 1 hour, which is more token-efficient than backgrounding + manual checkins.

### Recommended: Use poll-inbox.sh

The polling script handles the loop for you:

```bash
~/.claude/plugins/cache/.../a2a-communication/scripts/poll-inbox.sh my-agent 360 10
```

This polls for 1 hour (360 iterations × 10 seconds). The script prints the start timestamp to help with timeout discovery.

### Timeout and Background Handling

When running long commands in Claude Code:

- Default timeout is often 60 seconds; set `BASH_DEFAULT_TIMEOUT_MS` for longer
- When a command is backgrounded, you'll need to read the output file to check results
- The polling script prints a start timestamp - compare it to current time to learn your actual timeout

**Passive timeout discovery:**

If your polling script gets backgrounded mid-execution:

1. Note current time vs. the printed start timestamp to learn actual timeout
2. Kill the backgrounded task (it's still running but not useful for foreground interaction)
3. Start a fresh poll with fewer iterations sized to stay under the learned timeout

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
- Update your status in `active-agents.md` to `inactive`
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
- [ ] Find skill directories: `find ~/.claude/plugins/cache -type d -name a2a-communication` (pick highest version)
- [ ] Register using: `~/.claude/plugins/cache/.../a2a-communication/scripts/register-agent.sh <name> <desc> <caps> <dir>`
- [ ] Poll for any unread messages using poll-inbox.sh
- [ ] Read `~/a2a/active-agents.md` to see who else is active

---

## Related Skills

- **a2a:a2a-ralph-integration** - For autonomous operation with Ralph Wiggum loops
