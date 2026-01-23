---
description: Start a long-running a2a agent with Ralph loop
argument-hint: <agent-name> <repo-description>
---

Invoke /ralph-loop:ralph-loop with the following prompt:

"Act as a general purpose a2a agent $1 for $2. Use the a2a:a2a-communication skill to register yourself, send messages, and poll for incoming messages. If you have nothing to do, wait for messages using 1 hour polling loops. Restart the polling loop if it times out. Once you've worked on a task and the agents reaching out to you no longer need assistance, output TASK_COMPLETE and a stop hook will restart you with a fresh context window. If the stop hook outer loop needs to be aborted, output ABORT_LOOP."

Use --completion-promise ABORT_LOOP
