#!/usr/bin/env bash
# Send a message to another agent
#
# Usage: send-message.sh <from> <to> <subject> <expects-reply> [body]
#        If body is omitted, reads from stdin
#
# Example:
#   send-message.sh devspace-manager backend-api "Database ready" false "DB is up at localhost:5432"
#   echo "DB is up at localhost:5432" | send-message.sh devspace-manager backend-api "Database ready" false

set -euo pipefail

if [[ $# -lt 4 ]]; then
    echo "Usage: send-message.sh <from> <to> <subject> <expects-reply> [body]" >&2
    exit 1
fi

FROM="$1"
TO="$2"
SUBJECT="$3"
EXPECTS_REPLY="$4"
BODY="${5:-}"

A2A_DIR="${HOME}/a2a"
RECIPIENT_DIR="${A2A_DIR}/${TO}"

# Verify recipient exists
if [[ ! -d "${RECIPIENT_DIR}" ]]; then
    echo "Warning: recipient '${TO}' may not be registered (inbox doesn't exist)" >&2
fi

# Create recipient directory if needed
mkdir -p "${RECIPIENT_DIR}"

# Get timestamps
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
FILENAME_TIMESTAMP=$(date -u +"%Y-%m-%dT%H-%M-%SZ")

# Create subject slug (lowercase, hyphens, brief)
SUBJECT_SLUG=$(echo "${SUBJECT}" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd '[:alnum:]-' | cut -c1-50)

FILENAME="${FILENAME_TIMESTAMP}-${SUBJECT_SLUG}.md"
FILEPATH="${RECIPIENT_DIR}/${FILENAME}"

# Read body from stdin if not provided as argument
if [[ -z "${BODY}" ]]; then
    BODY=$(cat)
fi

# Write the message
cat > "${FILEPATH}" << EOF
---
from: ${FROM}
to: ${TO}
timestamp: ${TIMESTAMP}
subject: ${SUBJECT}
expects-reply: ${EXPECTS_REPLY}
---

${BODY}
EOF

echo "Sent message to ${TO}: ${FILEPATH}"
