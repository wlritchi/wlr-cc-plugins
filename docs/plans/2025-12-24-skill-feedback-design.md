# Skill Feedback System Design

## Overview

A feedback capture system that allows agents to report skill improvements based on user corrections during skill execution. When users make corrections that suggest general preferences (rather than project-specific customizations), the agent offers to open a PR improving the skill.

## Architecture

### Plugin Structure

New plugin `skill-feedback` at the marketplace root:

```
wlr-cc-plugins/
├── .claude-plugin/
│   └── marketplace.json        # Add skill-feedback plugin entry
├── opinionated-setup/
│   └── ...
└── skill-feedback/
    ├── .claude-plugin/
    │   └── plugin.json
    └── skills/
        └── reporting-feedback/
            └── SKILL.md
```

### Two-Part Design

1. **Copypasta section** - Short feedback instructions added to each skill that wants feedback capability. Handles the user-facing interaction (recognizing preferences, offering to report).

2. **reporting-feedback skill** - Sub-agent skill that handles the technical work of opening a PR via GitHub API.

## Copypasta Section (for regular skills)

Add to the end of each skill:

```markdown
## Feedback (Optional)

If the user directed corrections that suggest general preferences rather than
project-specific customizations, proactively offer to report feedback.

**Signals to watch for:** "always", "we should", "I prefer", "by default",
or corrections the user applies without explaining why (suggesting it's obvious to them).

**When detected:**
1. Summarize what you understood as the general preference
2. Ask: "Would you like me to open a PR suggesting changes to this skill based on
   your feedback about [topic]? (I can include other feedback too if there's more.)"
3. If yes: Spawn a sub-agent with `skill-feedback:reporting-feedback`, passing:
   - This skill's identifier
   - Summary of feedback/preferences
   - Relevant conversation context showing the corrections
4. Report the PR number to the user when the sub-agent completes
```

## reporting-feedback Skill

### Input Expected

The spawning agent provides:
- **Skill identifier** (e.g., `opinionated-setup:setup-python-project`)
- **Feedback summary** - What preferences or corrections were identified
- **Conversation context** - Relevant excerpt showing the user's corrections

### Process

1. **Parse skill path** - Convert `plugin:skill` to file path: `{plugin}/skills/{skill}/SKILL.md`

2. **Fetch current content** via GitHub API to temp file

3. **Read and edit** using Claude Code's Read/Edit tools for precise, minimal changes

4. **Create branch** via GitHub API from main

5. **Commit changes** via GitHub API

6. **Open PR** with context about the feedback source

7. **Return PR number** to spawning agent

### Clarifications

If feedback is ambiguous, use AskUserQuestion to clarify with the user rather than guessing.

### Meta-Feedback

The reporting-feedback skill handles its own feedback inline - if issues are encountered during execution, include improvements in the same PR rather than spawning another sub-agent.

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Skill location | Separate `skill-feedback` plugin | Shared across all plugins in marketplace |
| User interaction | Copypasta in each skill | Simple, customizable per skill |
| PR mechanism | GitHub API only | No local clone needed, works from any directory |
| File editing | Claude Code Edit tool via temp file | Precise diffs, avoids accidental rewrites |
| Repo reference | Hardcoded `wlritchi/wlr-cc-plugins` | Simple; fork-based contributions deferred |
| Meta-feedback | One level, inline | Avoids infinite recursion while allowing improvement |
| Ambiguity handling | AskUserQuestion | Sub-agent can clarify with user |

## Files to Create

1. `skill-feedback/.claude-plugin/plugin.json`
2. `skill-feedback/skills/reporting-feedback/SKILL.md`

## Files to Modify

1. `.claude-plugin/marketplace.json` - Add skill-feedback plugin
2. `opinionated-setup/skills/setup-python-project/SKILL.md` - Add feedback section
