#!/usr/bin/env bash
# Mark a message as read by creating a .seen file
#
# Usage: mark-read.sh <message-path>
#
# Example:
#   mark-read.sh ~/a2a/my-agent/2026-01-16T10-30-00Z-hello.md

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: mark-read.sh <message-path>" >&2
    exit 1
fi

MESSAGE_PATH="$1"

if [[ ! -f "${MESSAGE_PATH}" ]]; then
    echo "Error: message file not found: ${MESSAGE_PATH}" >&2
    exit 1
fi

touch "${MESSAGE_PATH}.seen"
echo "Marked as read: ${MESSAGE_PATH}"
