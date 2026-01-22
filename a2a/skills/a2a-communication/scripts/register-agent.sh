#!/usr/bin/env bash
# Register an agent with the a2a system
#
# Usage: register-agent.sh <agent-name> <description> <capabilities> <working-dir>
#
# Example:
#   register-agent.sh devspace-manager "Manages devspace environment" "k8s, docker" ~/repos/infra

set -euo pipefail

if [[ $# -lt 4 ]]; then
    echo "Usage: register-agent.sh <agent-name> <description> <capabilities> <working-dir>" >&2
    exit 1
fi

AGENT_NAME="$1"
DESCRIPTION="$2"
CAPABILITIES="$3"
WORKING_DIR="$4"

A2A_DIR="${HOME}/a2a"
AGENTS_FILE="${A2A_DIR}/active-agents.md"

# Create a2a directory and agent inbox
mkdir -p "${A2A_DIR}/${AGENT_NAME}"

# Initialize active-agents.md if it doesn't exist
if [[ ! -f "${AGENTS_FILE}" ]]; then
    echo "# Active Agents" > "${AGENTS_FILE}"
    echo "" >> "${AGENTS_FILE}"
fi

# Get current timestamp
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Append agent registration
cat >> "${AGENTS_FILE}" << EOF

## ${AGENT_NAME}

${DESCRIPTION}

**Capabilities:** ${CAPABILITIES}
**Working in:** ${WORKING_DIR}
**Started:** ${TIMESTAMP}
**Status:** active
EOF

echo "Registered agent '${AGENT_NAME}' at ${TIMESTAMP}"
