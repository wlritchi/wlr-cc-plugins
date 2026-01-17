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

## Directory Structure

```
~/a2a/
├── active-agents.md              # Registry of all agents
├── {agent-name}/                 # Inbox for each agent
│   ├── {timestamp}-{subject}.md  # Messages
│   └── {timestamp}-{subject}.md.seen  # Read markers (0-byte)
```

## Quick Reference

| Action | Command |
|--------|---------|
| Register | Append to `~/a2a/active-agents.md`, create inbox dir |
| Send message | Write to `~/a2a/{recipient}/` |
| Check inbox | List `*.md` without `.seen` marker |
| Mark read | `touch {message}.seen` |
| Watch for messages | `fswatch -1` or `inotifywait` or poll with `find` |

---

## On Startup: Register Yourself

When starting as an agent that may communicate with others:

### 1. Choose your agent name

Use kebab-case, descriptive of your role:
- `devspace-manager`
- `backend-api`
- `frontend-app`
- `pa` (personal assistant)

### 2. Create your inbox

```bash
mkdir -p ~/a2a/{your-agent-name}
```

### 3. Register in active-agents.md

Append a section to `~/a2a/active-agents.md`:

```markdown
## {your-agent-name}

{Brief description of your purpose and what you're working on.}

**Capabilities:** {what you can help with}
**Working in:** {repo or directory, if applicable}
**Started:** {ISO 8601 timestamp}
**Status:** active
```

Example:

```markdown
## devspace-manager

Manages the devspace development environment. Handles database provisioning,
service deployment, port forwarding, and environment health checks.

**Capabilities:** devspace operations, k8s/docker, environment troubleshooting
**Working in:** ~/repos/infrastructure
**Started:** 2026-01-16T09:00:00Z
**Status:** active
```

---

## Sending Messages

### Message format

Write markdown files with YAML frontmatter:

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

- Timestamp: ISO 8601 with hyphens for filesystem safety (e.g., `2026-01-16T10-30-00Z`)
- Subject slug: lowercase, hyphens, brief (e.g., `db-ready`, `schema-change`)

### Before sending

Verify the recipient exists:

```bash
[ -d ~/a2a/{recipient} ] || echo "Warning: recipient may not be registered"
```

### Example: Sending a notification

```bash
cat > ~/a2a/backend-api/2026-01-16T10-30-00Z-db-ready.md << 'EOF'
---
from: devspace-manager
to: backend-api
timestamp: 2026-01-16T10:30:00Z
subject: Database connection ready
expects-reply: false
---

The devspace DB is now available at `postgres://dev:dev@localhost:5432/app`.

Connection verified and migrations applied. You can proceed with API integration.
EOF
```

---

## Checking Your Inbox

### Find unread messages

```bash
for f in ~/a2a/{your-agent-name}/*.md; do
  [ -f "$f" ] && [ ! -f "$f.seen" ] && echo "$f"
done
```

### Mark a message as read

```bash
touch "{message-path}.seen"
```

### Process messages

When you find unread messages:

1. Read the message content
2. Determine if action is needed
3. If `expects-reply: true`, prioritize responding
4. Mark as read: `touch {message}.seen`

---

## Watching for New Messages

When blocked or idle, watch your inbox for new messages.

### Using fswatch (macOS)

```bash
fswatch -1 -r ~/a2a/{your-agent-name}/
```

### Using inotifywait (Linux)

```bash
inotifywait -e create ~/a2a/{your-agent-name}/
```

### Fallback: Polling with find

```bash
# Create timestamp marker
touch /tmp/{your-agent-name}-last-check

# Sleep, then check for new files
sleep 300  # 5 minutes
find ~/a2a/{your-agent-name}/ -name '*.md' -newer /tmp/{your-agent-name}-last-check -type f
```

### Timeout handling

- Set timeout as long as the harness allows (up to 1 hour if available)
- If the command goes to background due to timeout, kill it and relaunch
- After each timeout/wake, check for unread messages before waiting again

---

## The Agent Loop

When running as a long-lived agent:

```
┌─────────────────────────────────────────────────────────┐
│  1. Do primary work (if any pending tasks)              │
│  2. Check inbox for unread messages                     │
│  3. Process any unread messages                         │
│  4. If blocked (waiting for reply / no work):           │
│     └─> Watch inbox with long timeout                   │
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
- [ ] Create inbox: `mkdir -p ~/a2a/{name}`
- [ ] Register in `~/a2a/active-agents.md`
- [ ] Check for any unread messages in your inbox
- [ ] Read `active-agents.md` to see who else is active

---

## Related Skills

- **a2a:a2a-ralph-integration** - For autonomous operation with Ralph Wiggum loops
