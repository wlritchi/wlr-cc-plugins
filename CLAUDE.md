# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This is a Claude Code plugins marketplace repository containing multiple plugins. Each plugin provides "skills" (specialized prompt templates) that extend Claude Code's capabilities. This is NOT a traditional Python or JavaScript project—it's a content-based repository of markdown skill files.

## Repository Structure

```
├── .claude-plugin/marketplace.json    # Central registry of all plugins
├── {plugin}/
│   ├── .claude-plugin/plugin.json     # Plugin manifest (name, version, author)
│   ├── skills/{skill-name}/SKILL.md   # Skill prompt templates with YAML frontmatter
│   └── commands/{command}.md          # Thin wrappers that invoke skills
├── scripts/
│   └── bump-versions.py               # Automatic version bumping (PEP 723 script)
└── docs/plans/                        # Design documents
```

## Plugins

| Plugin | Description |
|--------|-------------|
| **a2a** | Agent-to-agent communication framework using filesystem-based messaging (`~/a2a/`) |
| **opinionated-setup** | Python project and Dockerfile setup templates (uv + hatchling toolchain) |
| **skill-feedback** | Captures user corrections to skills and offers to open improvement PRs |

## Development Commands

**Version bumping** (runs automatically in CI, but can be run locally):
```bash
ANTHROPIC_API_KEY=sk-ant-... ./scripts/bump-versions.py
```

**Update script dependencies:**
```bash
uv lock --script scripts/bump-versions.py                   # Lock
uv lock --script --upgrade scripts/bump-versions.py         # Upgrade
```

## Writing Skills

Skills are markdown files with YAML frontmatter located in `{plugin}/skills/{skill-name}/SKILL.md`:

```markdown
---
name: my-skill
description: Use when doing X, or when Y happens
---

# Skill Title

Instructions for Claude Code when this skill is invoked...
```

Commands in `{plugin}/commands/{command}.md` are thin wrappers that invoke skills:

```markdown
---
description: Short description for the command
---

Invoke the {plugin}:{skill-name} skill and follow it exactly as presented to you
```

## Version Management

- Versions are tracked in both `{plugin}/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`
- CI automatically bumps versions on push/merge to main using Claude Haiku to analyze changes
- Bump type (patch/minor/major) is determined by analyzing the diff

## Architecture Notes

- **A2A messaging**: Uses filesystem-based communication with markdown files containing YAML frontmatter for metadata
- **Feedback loop**: Skills include sections that detect user corrections and offer to open PRs for improvements
- **No build step**: Skills are used directly as markdown—test by invoking them with Claude Code
