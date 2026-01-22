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

# Validate agent name format
if [[ ! "${AGENT_NAME}" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "Error: agent name must contain only alphanumeric characters, underscores, or hyphens" >&2
    exit 1
fi

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

# Check if agent is already registered
if grep -q "^## ${AGENT_NAME}$" "${AGENTS_FILE}" 2>/dev/null; then
    # Extract existing values for comparison
    # Get the section for this agent (from ## to next ## or EOF)
    EXISTING_SECTION=$(sed -n "/^## ${AGENT_NAME}$/,/^## /p" "${AGENTS_FILE}" | head -n -1)
    if [[ -z "${EXISTING_SECTION}" ]]; then
        # Agent is last in file, no trailing ##
        EXISTING_SECTION=$(sed -n "/^## ${AGENT_NAME}$/,\$p" "${AGENTS_FILE}")
    fi

    # Extract existing field values
    EXISTING_DESC=$(echo "${EXISTING_SECTION}" | sed -n '3p')
    EXISTING_CAPS=$(echo "${EXISTING_SECTION}" | grep '^\*\*Capabilities:\*\*' | sed 's/\*\*Capabilities:\*\* //')
    EXISTING_DIR=$(echo "${EXISTING_SECTION}" | grep '^\*\*Working in:\*\*' | sed 's/\*\*Working in:\*\* //')

    # Check for field changes and warn
    CHANGES=()
    if [[ "${EXISTING_DESC}" != "${DESCRIPTION}" ]]; then
        CHANGES+=("description: '${EXISTING_DESC}' -> '${DESCRIPTION}'")
    fi
    if [[ "${EXISTING_CAPS}" != "${CAPABILITIES}" ]]; then
        CHANGES+=("capabilities: '${EXISTING_CAPS}' -> '${CAPABILITIES}'")
    fi
    if [[ "${EXISTING_DIR}" != "${WORKING_DIR}" ]]; then
        CHANGES+=("working-dir: '${EXISTING_DIR}' -> '${WORKING_DIR}'")
    fi

    if [[ ${#CHANGES[@]} -gt 0 ]]; then
        echo "Warning: re-registering '${AGENT_NAME}' with changed fields:" >&2
        for change in "${CHANGES[@]}"; do
            echo "  - ${change}" >&2
        done
    fi

    # Remove the existing entry (from ## agent-name to next ## or EOF)
    # Use a temp file for safety
    TEMP_FILE=$(mktemp)
    # Remove the agent section, being careful with the last agent case
    awk -v agent="## ${AGENT_NAME}" '
        BEGIN { skip = 0 }
        $0 == agent { skip = 1; next }
        /^## / && skip { skip = 0 }
        !skip { print }
    ' "${AGENTS_FILE}" > "${TEMP_FILE}"
    mv "${TEMP_FILE}" "${AGENTS_FILE}"

    echo "Updating registration for '${AGENT_NAME}' at ${TIMESTAMP}"
else
    echo "Registered agent '${AGENT_NAME}' at ${TIMESTAMP}"
fi

# Append agent registration
cat >> "${AGENTS_FILE}" << EOF

## ${AGENT_NAME}

${DESCRIPTION}

**Capabilities:** ${CAPABILITIES}
**Working in:** ${WORKING_DIR}
**Started:** ${TIMESTAMP}
**Status:** active
EOF
