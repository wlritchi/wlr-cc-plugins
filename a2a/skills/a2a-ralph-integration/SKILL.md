---
name: a2a-ralph-integration
description: Use when setting up an autonomous long-running agent with Ralph Wiggum loops and A2A messaging
---

# A2A + Ralph Wiggum Integration

Combines [A2A messaging](../a2a-communication/SKILL.md) with [Ralph Wiggum loops](https://github.com/anthropics/claude-code/blob/main/plugins/ralph-wiggum/README.md) for fully autonomous agent operation.

**REQUIRED:** Understand `a2a:a2a-communication` before using this skill.

## When to Use

- Running agents overnight or unattended
- Need automatic restart on crash/timeout
- Want hands-off operation with persistent state

## Standalone vs Ralph Mode

| Mode | Supervision | Restart | Best for |
|------|-------------|---------|----------|
| Standalone | Manual | Manual | Interactive sessions, debugging |
| Ralph | Automatic | Automatic | Long-running workers, overnight ops |

## Ralph Loop Template

```bash
/ralph-loop "You are the {agent-name} agent.

Register in ~/a2a/active-agents.md if not already registered, then loop:
1. Check inbox for unread messages, process any found
2. Do any pending work for your role
3. Watch for new messages (use longest available timeout)
4. Repeat

Output <promise>SHUTDOWN</promise> only when the human explicitly tells you to shut down." \
  --max-iterations 1000
```

## What Ralph Provides

- **Automatic restart** on crash or timeout
- **Persistence** across iterations (state lives in `~/a2a/` files)
- **Iteration limits** to prevent runaway loops
- **Promise-based shutdown** for graceful exit

## Best Practices

1. Always register in `active-agents.md` on first iteration
2. Use file-based state — Ralph restarts lose in-memory state
3. Set `--max-iterations` appropriate to expected runtime
4. Use `<promise>SHUTDOWN</promise>` for clean exit, not crash
