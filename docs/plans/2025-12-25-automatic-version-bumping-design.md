# Automatic Version Bumping for Plugin Changes

## Overview

This design describes a GitHub Actions workflow that automatically bumps plugin versions when changes are merged or pushed to the main branch. The workflow uses Claude to analyze changes and determine appropriate semantic version bumps.

## Goals

- Automatically detect which plugins have changes since their last version bump
- Use Claude to analyze the nature of changes and determine bump type (patch/minor/major)
- Update versions in both `plugin.json` and `marketplace.json` files
- Commit and push version bumps atomically with fast-forward-only pushes
- Handle race conditions gracefully when multiple commits arrive

## Non-Goals

- Manual version bumping (this remains available but not required)
- Versioning individual skills within plugins
- Creating git tags for version releases (may be added later)
- Publishing to package registries

## Workflow Trigger

The workflow runs on two events:

1. **Pull request merged to main:**
   ```yaml
   on:
     pull_request:
       types: [closed]
       branches: [main]
   ```
   With a condition: `if: github.event.pull_request.merged == true`

2. **Direct push to main:**
   ```yaml
   on:
     push:
       branches: [main]
   ```

The workflow requires elevated permissions:
- `contents: write` - to push version bump commits back to main
- `id-token: write` - for OIDC authentication (if needed)

## Version Tracking and Change Detection

### Finding the Last Version Bump

For each plugin, we determine the baseline commit by finding the last commit that modified the version field in either:
- `.claude-plugin/marketplace.json`
- `{plugin-dir}/.claude-plugin/plugin.json`

The Python script uses `gitpython` to:
1. Get the commit history for these files
2. For each commit, check if it actually modified the version field (not just other fields)
3. Use the most recent version-changing commit as the baseline

### Detecting Changes

Once we have the baseline commit, we check if the plugin directory has any changes:
```python
repo = git.Repo('.')
diff = repo.commit(baseline_commit).diff(repo.head.commit, paths=f'{plugin_dir}/')
has_changes = len(diff) > 0
```

### Gathering Context for Claude

For each plugin with changes, we collect:
1. **Commit messages:** All commit messages since the baseline
2. **Full diff:** The complete diff of all changes to the plugin directory
3. **Current version:** To provide context for what version comes next

This gives Claude comprehensive context to make informed decisions.

## Claude API Integration

### Dependencies

The workflow uses a Python script with inline PEP 723 metadata:

```python
#!/usr/bin/env -S uv run -qs
# /// script
# dependencies = [
#   "anthropic",
#   "gitpython",
# ]
# ///
```

Dependencies are locked using `uv lock --script bump-versions.py`, which creates a `bump-versions.py.lock` file. This protects against supply chain attacks while allowing the script to run with elevated GitHub permissions.

### API Call Structure

For each plugin with changes, the script makes a single API call using Claude Haiku (fast and cost-effective):

```python
import anthropic
import os

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

prompt = f"""Analyze these changes to the "{plugin_name}" Claude Code plugin and determine the appropriate semantic version bump.

Current version: {current_version}

Commit messages since last bump:
{commit_messages}

Full diff:
{diff_content}

Context: This is a Claude Code plugin consisting primarily of skills (prompt templates) and documentation for Claude.

Respond with ONLY one word: "patch", "minor", or "major"
- patch: Bug fixes, typo corrections, small refinements
- minor: New features, significant improvements (default if uncertain)
- major: Breaking changes, incompatible modifications

Version bump type:"""

response = client.messages.create(
    model="claude-haiku-4-20250514",
    max_tokens=10,
    messages=[{"role": "user", "content": prompt}]
)

bump_type = response.content[0].text.strip().lower()
```

### Response Handling

The script validates that the response is one of: `"patch"`, `"minor"`, or `"major"`.

If the response is unclear, invalid, or Claude expresses uncertainty, default to `"minor"` (appropriate for a documentation-heavy repo where most changes are functional improvements).

## Version Bumping Logic

### Semantic Versioning

```python
def bump_version(current: str, bump_type: str) -> str:
    """Bump a semantic version string based on bump type."""
    major, minor, patch = map(int, current.split('.'))

    if bump_type == 'major':
        return f"{major + 1}.0.0"
    elif bump_type == 'minor':
        return f"{major}.{minor + 1}.0"
    else:  # patch or default
        return f"{major}.{minor}.{patch + 1}"
```

### Updating Files

For each plugin requiring a version bump, update two files:

1. **Plugin metadata:** `{plugin-dir}/.claude-plugin/plugin.json`
2. **Marketplace registry:** `.claude-plugin/marketplace.json`

The script uses Python's `json` module to:
- Parse the JSON files
- Update the version field
- Write back with preserved formatting (using `indent=2`)

### Batch Processing

All version bumps are calculated first, then all files are updated in a single pass. This ensures consistency when multiple plugins need bumping - they all get updated together before committing.

## Commit and Push Strategy

### Git Configuration

The workflow configures git with a bot identity:
```yaml
- name: Configure git
  run: |
    git config user.name "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
```

### Commit Message Format

Single commit with all version bumps:
```
chore: bump versions

- opinionated-setup: 0.1.0 -> 0.2.0 (minor)
- skill-feedback: 0.1.0 -> 0.1.1 (patch)
```

### Fast-Forward-Only Push

The script attempts to push with fast-forward semantics:

```python
repo = git.Repo('.')

# Stage all changed files
changed_files = ['marketplace.json']
for plugin_name in bumped_plugins:
    changed_files.append(f'{plugin_name}/.claude-plugin/plugin.json')

repo.index.add(changed_files)
repo.index.commit(commit_message)

# Attempt fast-forward push
try:
    repo.remotes.origin.push('main:main', force_with_lease=False)
    print("✓ Version bump pushed successfully")
except git.GitCommandError as e:
    if "non-fast-forward" in str(e) or "rejected" in str(e):
        print("⚠ Push rejected (another commit arrived). Exiting gracefully.")
        sys.exit(0)  # Exit success - next run will handle it
    else:
        raise  # Actual error, fail the workflow
```

### Handling Race Conditions

If the push is rejected because another commit arrived (or another workflow run finished first):
- The workflow exits gracefully with exit code 0
- The next workflow trigger will pick up all accumulated changes
- No retry logic needed - this keeps the implementation simple

### No Changes Scenario

If no plugins have changes requiring version bumps, the script exits early without creating a commit.

## Setup Instructions

### Creating the Anthropic API Key

1. Go to https://console.anthropic.com/settings/keys
2. Click "Create Key"
3. Give it a name like "GitHub Actions - Version Bumping"
4. Copy the key (starts with `sk-ant-...`)

### Adding the Secret to GitHub

1. Go to your repository on GitHub
2. Navigate to Settings → Secrets and variables → Actions
3. Click "New repository secret"
4. Name: `ANTHROPIC_API_KEY`
5. Value: Paste the API key from above
6. Click "Add secret"

### File Structure

The implementation creates:
- `.github/workflows/version-bump.yml` - The workflow definition
- `scripts/bump-versions.py` - Python script with inline dependencies
- `scripts/bump-versions.py.lock` - uv lockfile for dependency pinning

### Initial Lockfile Generation

After creating the script, run locally:
```bash
uv lock --script scripts/bump-versions.py
```

Commit both `bump-versions.py` and `bump-versions.py.lock` together.

### Testing

Test the workflow by:
1. Making a change to a plugin
2. Pushing to main or merging a PR
3. Observing the workflow run in the Actions tab
4. Verifying the automatic version bump commit

## Workflow YAML Overview

```yaml
name: Automatic Version Bumping

on:
  pull_request:
    types: [closed]
    branches: [main]
  push:
    branches: [main]

jobs:
  bump-versions:
    if: github.event_name == 'push' || github.event.pull_request.merged == true
    runs-on: ubuntu-latest
    permissions:
      contents: write
      id-token: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0  # Need full history to find baseline commits

      - name: Setup uv
        uses: astral-sh/setup-uv@v4

      - name: Configure git
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"

      - name: Run version bump script
        run: ./scripts/bump-versions.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

## Security Considerations

### Supply Chain Protection

- Dependencies are pinned using `uv.lock` for the script
- The lockfile is committed and reviewed in PRs
- Manual updates required until Dependabot/Renovate support uv's PEP 723 lockfiles

### Permission Minimization

The workflow only has `contents: write` permission, limited to what's needed to push version bump commits.

### API Key Security

The `ANTHROPIC_API_KEY` is stored as a GitHub secret and only exposed to the workflow environment.

## Future Enhancements

- Add git tags for each plugin version (e.g., `opinionated-setup-v0.2.0`)
- Support for pre-release versions (e.g., `0.2.0-beta.1`)
- Notification on version bumps (GitHub Discussions, Discord, etc.)
- Plugin publishing automation after version bumps
- Support for changelog generation
