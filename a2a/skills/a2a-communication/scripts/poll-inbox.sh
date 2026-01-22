#!/usr/bin/env bash
# Poll an agent's inbox for unread messages
#
# Usage: poll-inbox.sh <agent-name> <max-iterations> <delay-seconds>
#
# Returns the first unread message found (outputs its path and content).
# Does NOT mark the message as read - caller should use mark-read.sh after processing.
# Exits with code 0 if a message is found, code 1 if no messages after all iterations.
#
# Example:
#   poll-inbox.sh my-agent 30 10   # Poll 30 times with 10s delay (5 minutes total)

set -euo pipefail

if [[ $# -lt 3 ]]; then
    echo "Usage: poll-inbox.sh <agent-name> <max-iterations> <delay-seconds>" >&2
    exit 1
fi

AGENT_NAME="$1"
MAX_ITERATIONS="$2"
DELAY_SECONDS="$3"

# Validate agent name format
if [[ ! "${AGENT_NAME}" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "Error: agent name must contain only alphanumeric characters, underscores, or hyphens" >&2
    exit 1
fi

# Validate max-iterations is a positive integer
if ! [[ "${MAX_ITERATIONS}" =~ ^[0-9]+$ ]] || [[ "${MAX_ITERATIONS}" -lt 1 ]]; then
    echo "Error: max-iterations must be a positive integer" >&2
    exit 1
fi

# Validate delay-seconds is a non-negative integer
if ! [[ "${DELAY_SECONDS}" =~ ^[0-9]+$ ]]; then
    echo "Error: delay-seconds must be a non-negative integer" >&2
    exit 1
fi

A2A_DIR="${HOME}/a2a"
INBOX_DIR="${A2A_DIR}/${AGENT_NAME}"

if [[ ! -d "${INBOX_DIR}" ]]; then
    echo "Error: inbox directory not found: ${INBOX_DIR}" >&2
    echo "Have you registered this agent?" >&2
    exit 1
fi

echo "Polling inbox for ${AGENT_NAME} (max ${MAX_ITERATIONS} iterations, ${DELAY_SECONDS}s delay)"
echo "Poll started at $(date +%s)"

# Enable nullglob so empty globs expand to nothing instead of the literal pattern
shopt -s nullglob

for ((i=1; i<=MAX_ITERATIONS; i++)); do
    # Check for unread messages
    for f in "${INBOX_DIR}"/*.md; do
        if [[ ! -f "$f.seen" ]]; then
            echo "--- Found unread message (iteration ${i}) ---"
            echo "Path: $f"
            echo "--- Content ---"
            cat "$f"
            exit 0
        fi
    done

    # Sleep unless this is the last iteration
    if [[ $i -lt $MAX_ITERATIONS ]]; then
        sleep "${DELAY_SECONDS}"
    fi
done

echo "No unread messages found after ${MAX_ITERATIONS} iterations"
exit 1
