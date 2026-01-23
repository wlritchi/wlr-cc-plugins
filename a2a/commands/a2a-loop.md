---
description: Start a long-running a2a agent with Ralph loop
argument-hint: <agent-name> <repo-description>
---

Invoke /ralph-loop:ralph-loop with the following prompt:

"Act as a general purpose a2a agent $1 for $2. Use the a2a:a2a-communication skill to register yourself, send messages, and poll for incoming messages. If you have nothing to do, wait for messages using 1 hour polling loops. Restart the polling loop if it times out. Once you've worked on a task and the agents reaching out to you no longer need assistance, output TASK_COMPLETE and a stop hook will restart you with a fresh context window. If the stop hook outer loop needs to be aborted, output ABORT_LOOP."

Use --completion-promise ABORT_LOOP

## Troubleshooting

If you encounter bash quoting issues when running `/ralph-loop:ralph-loop` (e.g., parse errors, unexpected token errors, or the command failing to start properly), try these approaches before falling back to running a2a without ralph-loop:

1. **Escape inner quotes**: Use `\"` for quotes inside the prompt string
2. **Use single quotes for the outer wrapper**: Wrap the entire prompt in single quotes and use double quotes inside
3. **Use a heredoc**: Pass the prompt via stdin or a heredoc if the Skill tool supports it
4. **Simplify the prompt**: Remove special characters or break the prompt into simpler parts

Only fall back to running a2a without ralph-loop if you've tried at least 2-3 different quoting approaches and they all fail.
