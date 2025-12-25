# Version Bump Script

Automatically bumps plugin versions based on changes since the last version bump.

## How It Works

1. Reads `.claude-plugin/marketplace.json` to discover plugins
2. For each plugin, finds the last commit that changed its version field
3. Detects if there are changes to the plugin directory since that commit
4. Uses Claude API to analyze changes and determine bump type (patch/minor/major)
5. Updates version fields in both `plugin.json` and `marketplace.json`
6. Creates a single commit with all version bumps
7. Pushes with fast-forward-only semantics

## Usage

### Locally

```bash
ANTHROPIC_API_KEY=sk-ant-... ./scripts/bump-versions.py
```

### In GitHub Actions

The workflow runs automatically on:
- PR merges to main
- Direct pushes to main

Configure the `ANTHROPIC_API_KEY` secret in repository settings.

## Dependencies

Dependencies are managed via PEP 723 inline script metadata:
- `anthropic` - Claude API client
- `gitpython` - Git operations

Locked with `uv lock --script scripts/bump-versions.py`

## Updating Dependencies

```bash
# Upgrade all dependencies
uv lock --script --upgrade scripts/bump-versions.py

# Commit the updated lockfile
git add scripts/bump-versions.py.lock
git commit -m "chore: update version bump script dependencies"
```
