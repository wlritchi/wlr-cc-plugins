#!/usr/bin/env bash
# Give guidance.md to subagents. SessionStart output reaches only the main
# session, and SubagentStart adds plain stdout to no context, so this hook
# returns it as additionalContext.
# Dependencies: jq
set -euo pipefail

agent_type=$(jq -r '.agent_type // empty')

# A fork has the parent context, which already has the guidance.
if [[ "$agent_type" == "fork" ]]; then
    exit 0
fi

jq -n --rawfile guidance "$(dirname "$0")/guidance.md" \
    '{hookSpecificOutput: {hookEventName: "SubagentStart", additionalContext: $guidance}}'
