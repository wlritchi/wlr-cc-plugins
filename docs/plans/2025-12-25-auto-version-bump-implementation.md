# Automatic Version Bumping Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement a GitHub Actions workflow that automatically bumps plugin versions when changes are merged or pushed to main, using Claude API to determine appropriate bump types.

**Architecture:** Python script with PEP 723 dependencies uses gitpython to detect changes since last version bump, calls Claude Haiku API to analyze diffs and determine bump type, updates JSON files, and commits with fast-forward-only push.

**Tech Stack:** Python 3.12+, uv, gitpython, anthropic SDK, GitHub Actions

---

## Task 1: Create Python Script Structure

**Files:**
- Create: `scripts/bump-versions.py`

**Step 1: Create script with PEP 723 metadata and main structure**

Create `scripts/bump-versions.py`:

```python
#!/usr/bin/env -S uv run -qs
# /// script
# dependencies = [
#   "anthropic>=0.40.0",
#   "gitpython>=3.1.0",
# ]
# ///

"""Automatically bump plugin versions based on changes since last bump."""

import json
import os
import sys
from pathlib import Path
from typing import Optional

import anthropic
import git


def main() -> int:
    """Main entry point for version bumping script."""
    print("🔍 Analyzing repository for version bumps...")

    # Validate environment
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ Error: ANTHROPIC_API_KEY environment variable not set")
        return 1

    # Initialize git repo
    try:
        repo = git.Repo(".")
    except git.InvalidGitRepositoryError:
        print("❌ Error: Not a git repository")
        return 1

    print("✓ Environment validated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Step 2: Make script executable**

Run:
```bash
chmod +x scripts/bump-versions.py
```

**Step 3: Test script runs**

Run:
```bash
cd .worktrees/auto-version-bump
ANTHROPIC_API_KEY=dummy ./scripts/bump-versions.py
```

Expected output:
```
🔍 Analyzing repository for version bumps...
✓ Environment validated
```

Exit code: 0

**Step 4: Commit**

```bash
git add scripts/bump-versions.py
git commit -m "feat: add version bump script skeleton

Add Python script with PEP 723 dependencies for automatic version
bumping. Includes basic validation of environment and git repository.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 2: Implement Plugin Discovery

**Files:**
- Modify: `scripts/bump-versions.py`

**Step 1: Add function to load marketplace.json**

Add after imports:

```python
def load_marketplace_config() -> dict:
    """Load marketplace.json to get list of plugins."""
    marketplace_path = Path(".claude-plugin/marketplace.json")

    if not marketplace_path.exists():
        print(f"❌ Error: {marketplace_path} not found")
        sys.exit(1)

    with open(marketplace_path) as f:
        return json.load(f)


def get_plugins() -> list[dict]:
    """Get list of plugins from marketplace config."""
    config = load_marketplace_config()
    plugins = config.get("plugins", [])

    print(f"📦 Found {len(plugins)} plugins:")
    for plugin in plugins:
        print(f"  - {plugin['name']} (v{plugin['version']})")

    return plugins
```

**Step 2: Call from main**

Update `main()` function to call `get_plugins()` after validation:

```python
def main() -> int:
    """Main entry point for version bumping script."""
    print("🔍 Analyzing repository for version bumps...")

    # Validate environment
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ Error: ANTHROPIC_API_KEY environment variable not set")
        return 1

    # Initialize git repo
    try:
        repo = git.Repo(".")
    except git.InvalidGitRepositoryError:
        print("❌ Error: Not a git repository")
        return 1

    print("✓ Environment validated")

    # Load plugins
    plugins = get_plugins()

    return 0
```

**Step 3: Test plugin discovery**

Run:
```bash
ANTHROPIC_API_KEY=dummy ./scripts/bump-versions.py
```

Expected output:
```
🔍 Analyzing repository for version bumps...
✓ Environment validated
📦 Found 2 plugins:
  - opinionated-setup (v0.1.0)
  - skill-feedback (v0.1.0)
```

**Step 4: Commit**

```bash
git add scripts/bump-versions.py
git commit -m "feat: add plugin discovery from marketplace.json

Load and display plugins from marketplace configuration.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 3: Implement Last Version Bump Detection

**Files:**
- Modify: `scripts/bump-versions.py`

**Step 1: Add function to find last version bump commit**

Add function:

```python
def find_last_version_bump(repo: git.Repo, plugin_name: str) -> Optional[str]:
    """Find the last commit that bumped this plugin's version.

    Returns the commit SHA, or None if no version bump found.
    """
    # Files to check for version changes
    files_to_check = [
        ".claude-plugin/marketplace.json",
        f"{plugin_name}/.claude-plugin/plugin.json"
    ]

    # Get commit history for these files
    try:
        commits = list(repo.iter_commits(paths=files_to_check, max_count=100))
    except git.GitCommandError:
        return None

    # Check each commit to see if it actually changed the version
    for commit in commits:
        try:
            # Check the diff for this commit
            if commit.parents:
                parent = commit.parents[0]
                diffs = parent.diff(commit, paths=files_to_check, create_patch=True)

                for diff in diffs:
                    # Look for version field changes in the patch
                    if diff.diff and b'"version"' in diff.diff:
                        # Found a version change
                        return commit.hexsha
        except (IndexError, git.GitCommandError):
            continue

    return None
```

**Step 2: Test finding version bumps**

Add to main() after loading plugins:

```python
# Find last version bump for each plugin
print("\n🔎 Finding last version bumps...")
for plugin in plugins:
    last_bump = find_last_version_bump(repo, plugin["name"])
    if last_bump:
        print(f"  - {plugin['name']}: {last_bump[:8]}")
    else:
        print(f"  - {plugin['name']}: (no version bump found, using repo root)")
```

**Step 3: Run test**

Run:
```bash
ANTHROPIC_API_KEY=dummy ./scripts/bump-versions.py
```

Expected: Shows last version bump commit for each plugin (or "no version bump found")

**Step 4: Commit**

```bash
git add scripts/bump-versions.py
git commit -m "feat: implement last version bump detection

Find the last commit that changed a plugin's version field by
examining git history and patch diffs.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 4: Implement Change Detection

**Files:**
- Modify: `scripts/bump-versions.py`

**Step 1: Add function to detect changes**

Add function:

```python
def has_changes_since(repo: git.Repo, plugin_name: str, since_commit: Optional[str]) -> bool:
    """Check if plugin directory has changes since the given commit.

    If since_commit is None, checks against repo root.
    """
    plugin_path = plugin_name

    if since_commit is None:
        # No previous version bump, check if there are any commits for this plugin
        try:
            commits = list(repo.iter_commits(paths=plugin_path, max_count=1))
            return len(commits) > 0
        except git.GitCommandError:
            return False

    # Compare since_commit to HEAD
    try:
        since = repo.commit(since_commit)
        head = repo.head.commit

        if since == head:
            # Already at the version bump commit
            return False

        # Check for differences
        diffs = since.diff(head, paths=plugin_path)
        return len(diffs) > 0
    except (git.GitCommandError, git.BadName):
        return False


def get_changes_context(repo: git.Repo, plugin_name: str, since_commit: Optional[str]) -> dict:
    """Get commit messages and diff for changes since the given commit."""
    plugin_path = plugin_name

    if since_commit is None:
        # Get all history
        since_ref = None
    else:
        since_ref = since_commit

    # Get commit messages
    commit_messages = []
    try:
        if since_ref:
            commits = list(repo.iter_commits(f"{since_ref}..HEAD", paths=plugin_path))
        else:
            commits = list(repo.iter_commits("HEAD", paths=plugin_path))

        commit_messages = [f"- {c.summary}" for c in reversed(commits)]
    except git.GitCommandError:
        commit_messages = []

    # Get diff
    diff_text = ""
    try:
        if since_ref:
            since = repo.commit(since_ref)
            head = repo.head.commit
            diffs = since.diff(head, paths=plugin_path, create_patch=True)
        else:
            # Get diff from empty tree
            diffs = repo.head.commit.diff(git.NULL_TREE, paths=plugin_path, create_patch=True)

        diff_parts = []
        for diff in diffs:
            if diff.diff:
                diff_parts.append(diff.diff.decode('utf-8', errors='ignore'))

        diff_text = "\n".join(diff_parts)
    except (git.GitCommandError, git.BadName):
        diff_text = ""

    return {
        "commit_messages": "\n".join(commit_messages) if commit_messages else "(no commits)",
        "diff": diff_text if diff_text else "(no diff)"
    }
```

**Step 2: Update main to detect changes**

Update main() to detect and display changes:

```python
# Find plugins that need version bumps
print("\n🔎 Finding last version bumps...")
plugins_to_bump = []

for plugin in plugins:
    last_bump = find_last_version_bump(repo, plugin["name"])
    has_changes = has_changes_since(repo, plugin["name"], last_bump)

    if last_bump:
        print(f"  - {plugin['name']}: last bump at {last_bump[:8]}, changes: {has_changes}")
    else:
        print(f"  - {plugin['name']}: no version bump found, changes: {has_changes}")

    if has_changes:
        plugins_to_bump.append({
            "plugin": plugin,
            "last_bump": last_bump
        })

if not plugins_to_bump:
    print("\n✓ No plugins need version bumps")
    return 0

print(f"\n📝 {len(plugins_to_bump)} plugin(s) need version bumps")
```

**Step 3: Run test**

Run:
```bash
ANTHROPIC_API_KEY=dummy ./scripts/bump-versions.py
```

Expected: Shows which plugins have changes and need bumps

**Step 4: Commit**

```bash
git add scripts/bump-versions.py
git commit -m "feat: implement change detection for plugins

Detect which plugins have changes since their last version bump
and gather commit messages and diffs for analysis.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 5: Implement Version Bumping Logic

**Files:**
- Modify: `scripts/bump-versions.py`

**Step 1: Add version parsing and bumping functions**

Add functions:

```python
def parse_version(version: str) -> tuple[int, int, int]:
    """Parse semantic version string into (major, minor, patch)."""
    parts = version.split('.')
    if len(parts) != 3:
        raise ValueError(f"Invalid version format: {version}")

    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        raise ValueError(f"Invalid version format: {version}")


def bump_version(current: str, bump_type: str) -> str:
    """Bump a semantic version based on bump type.

    Args:
        current: Current version (e.g., "0.1.0")
        bump_type: One of "major", "minor", or "patch"

    Returns:
        New version string
    """
    major, minor, patch = parse_version(current)

    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        # Default to minor if uncertain
        return f"{major}.{minor + 1}.0"
```

**Step 2: Add test output**

Add to main() after detecting plugins to bump:

```python
# Test version bumping logic
print("\n🧪 Testing version bump logic:")
test_versions = [
    ("0.1.0", "patch", "0.1.1"),
    ("0.1.0", "minor", "0.2.0"),
    ("0.1.0", "major", "1.0.0"),
    ("1.2.3", "patch", "1.2.4"),
]

for current, bump_type, expected in test_versions:
    result = bump_version(current, bump_type)
    status = "✓" if result == expected else "✗"
    print(f"  {status} {current} + {bump_type} = {result} (expected {expected})")
```

**Step 3: Run test**

Run:
```bash
ANTHROPIC_API_KEY=dummy ./scripts/bump-versions.py
```

Expected: All version bump tests pass

**Step 4: Remove test output and commit**

Remove the test output section from main(), then commit:

```bash
git add scripts/bump-versions.py
git commit -m "feat: implement version parsing and bumping

Add functions to parse and bump semantic versions according to
patch/minor/major rules.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 6: Implement Claude API Integration

**Files:**
- Modify: `scripts/bump-versions.py`

**Step 1: Add function to analyze changes with Claude**

Add function:

```python
def analyze_changes_with_claude(
    client: anthropic.Anthropic,
    plugin_name: str,
    current_version: str,
    changes_context: dict
) -> str:
    """Use Claude to analyze changes and determine bump type.

    Returns: "major", "minor", or "patch"
    """
    commit_messages = changes_context["commit_messages"]
    diff = changes_context["diff"]

    # Truncate diff if too long (Claude has token limits)
    max_diff_length = 50000
    if len(diff) > max_diff_length:
        diff = diff[:max_diff_length] + "\n\n[... diff truncated ...]"

    prompt = f"""Analyze these changes to the "{plugin_name}" Claude Code plugin and determine the appropriate semantic version bump.

Current version: {current_version}

Commit messages since last bump:
{commit_messages}

Full diff:
{diff}

Context: This is a Claude Code plugin consisting primarily of skills (prompt templates) and documentation for Claude.

Respond with ONLY one word: "patch", "minor", or "major"
- patch: Bug fixes, typo corrections, small refinements
- minor: New features, significant improvements (default if uncertain)
- major: Breaking changes, incompatible modifications

Version bump type:"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-20250514",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}]
        )

        bump_type = response.content[0].text.strip().lower()

        # Validate response
        if bump_type not in ["patch", "minor", "major"]:
            print(f"  ⚠️  Claude returned unexpected value '{bump_type}', defaulting to 'minor'")
            return "minor"

        return bump_type
    except Exception as e:
        print(f"  ⚠️  Claude API error: {e}, defaulting to 'minor'")
        return "minor"
```

**Step 2: Update main to use Claude**

Update main() to analyze changes with Claude:

```python
# Analyze each plugin with Claude
client = anthropic.Anthropic(api_key=api_key)
bump_plan = []

print("\n🤖 Analyzing changes with Claude...")
for item in plugins_to_bump:
    plugin = item["plugin"]
    plugin_name = plugin["name"]
    current_version = plugin["version"]

    print(f"\n  Analyzing {plugin_name}...")

    # Get changes context
    changes = get_changes_context(repo, plugin_name, item["last_bump"])

    # Ask Claude
    bump_type = analyze_changes_with_claude(client, plugin_name, current_version, changes)
    new_version = bump_version(current_version, bump_type)

    print(f"    {current_version} → {new_version} ({bump_type})")

    bump_plan.append({
        "plugin_name": plugin_name,
        "current_version": current_version,
        "new_version": new_version,
        "bump_type": bump_type,
        "plugin_dir": plugin["source"]
    })

# Display summary
print("\n📋 Version bump plan:")
for plan in bump_plan:
    print(f"  - {plan['plugin_name']}: {plan['current_version']} → {plan['new_version']} ({plan['bump_type']})")
```

**Step 3: Test with real API key**

Run:
```bash
ANTHROPIC_API_KEY=<your-key> ./scripts/bump-versions.py
```

Expected: Shows Claude's analysis and version bump recommendations

**Step 4: Commit**

```bash
git add scripts/bump-versions.py
git commit -m "feat: integrate Claude API for change analysis

Use Claude Haiku to analyze plugin changes and determine
appropriate version bump types (patch/minor/major).

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 7: Implement JSON File Updates

**Files:**
- Modify: `scripts/bump-versions.py`

**Step 1: Add function to update JSON files**

Add function:

```python
def update_plugin_versions(bump_plan: list[dict]) -> list[str]:
    """Update version fields in plugin.json and marketplace.json.

    Returns list of modified file paths.
    """
    modified_files = []

    # Update individual plugin.json files
    for plan in bump_plan:
        plugin_json_path = Path(plan["plugin_dir"]) / ".claude-plugin" / "plugin.json"

        # Read plugin.json
        with open(plugin_json_path) as f:
            plugin_data = json.load(f)

        # Update version
        plugin_data["version"] = plan["new_version"]

        # Write back with formatting
        with open(plugin_json_path, "w") as f:
            json.dump(plugin_data, f, indent=2)
            f.write("\n")  # Add trailing newline

        modified_files.append(str(plugin_json_path))
        print(f"  ✓ Updated {plugin_json_path}")

    # Update marketplace.json
    marketplace_path = Path(".claude-plugin/marketplace.json")

    with open(marketplace_path) as f:
        marketplace_data = json.load(f)

    # Update versions for each plugin
    for plan in bump_plan:
        for plugin in marketplace_data.get("plugins", []):
            if plugin["name"] == plan["plugin_name"]:
                plugin["version"] = plan["new_version"]
                break

    # Write back
    with open(marketplace_path, "w") as f:
        json.dump(marketplace_data, f, indent=2)
        f.write("\n")

    modified_files.append(str(marketplace_path))
    print(f"  ✓ Updated {marketplace_path}")

    return modified_files
```

**Step 2: Call from main**

Add after displaying bump plan:

```python
# Update JSON files
print("\n📝 Updating version files...")
modified_files = update_plugin_versions(bump_plan)
```

**Step 3: Test file updates**

Run:
```bash
ANTHROPIC_API_KEY=<your-key> ./scripts/bump-versions.py
```

Check that JSON files are updated correctly:
```bash
git diff
```

Expected: Shows version field updates in JSON files

Reset changes:
```bash
git checkout -- .
```

**Step 4: Commit**

```bash
git add scripts/bump-versions.py
git commit -m "feat: implement JSON file version updates

Update version fields in both plugin.json and marketplace.json
files for bumped plugins.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 8: Implement Commit and Push Logic

**Files:**
- Modify: `scripts/bump-versions.py`

**Step 1: Add commit and push functions**

Add functions:

```python
def create_bump_commit(repo: git.Repo, bump_plan: list[dict], modified_files: list[str]) -> None:
    """Create commit for version bumps."""
    # Stage modified files
    repo.index.add(modified_files)

    # Build commit message
    message_lines = ["chore: bump versions", ""]

    for plan in bump_plan:
        line = f"- {plan['plugin_name']}: {plan['current_version']} → {plan['new_version']} ({plan['bump_type']})"
        message_lines.append(line)

    message_lines.extend([
        "",
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
        "",
        "Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
    ])

    commit_message = "\n".join(message_lines)

    # Create commit
    repo.index.commit(commit_message)
    print("\n✓ Created version bump commit")


def push_changes(repo: git.Repo) -> bool:
    """Push changes with fast-forward-only semantics.

    Returns True if push succeeded, False if rejected (another commit arrived).
    """
    try:
        # Push to main with no force
        origin = repo.remote("origin")
        push_info = origin.push("HEAD:main")[0]

        # Check if push was rejected
        if push_info.flags & push_info.ERROR:
            print("\n⚠️  Push rejected (another commit arrived). Exiting gracefully.")
            print("   (Next workflow run will pick up all changes)")
            return False

        print("\n✓ Version bump pushed successfully")
        return True

    except git.GitCommandError as e:
        error_msg = str(e).lower()
        if "non-fast-forward" in error_msg or "rejected" in error_msg:
            print("\n⚠️  Push rejected (another commit arrived). Exiting gracefully.")
            print("   (Next workflow run will pick up all changes)")
            return False
        else:
            # Actual error
            print(f"\n❌ Push error: {e}")
            raise
```

**Step 2: Update main to commit and push**

Add after updating files:

```python
# Create commit
create_bump_commit(repo, bump_plan, modified_files)

# Push changes
success = push_changes(repo)

return 0 if success else 0  # Exit 0 even if push rejected
```

**Step 3: Test commit creation (don't push)**

Temporarily comment out the `push_changes()` call and run:

```bash
ANTHROPIC_API_KEY=<your-key> ./scripts/bump-versions.py
```

Check commit:
```bash
git log -1 --stat
```

Expected: Shows commit with updated JSON files and proper message format

Reset:
```bash
git reset --hard HEAD~1
```

**Step 4: Uncomment push and commit**

```bash
git add scripts/bump-versions.py
git commit -m "feat: implement commit and push logic

Create version bump commits and push with fast-forward-only
semantics. Gracefully handle race conditions when push is rejected.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 9: Create GitHub Actions Workflow

**Files:**
- Create: `.github/workflows/version-bump.yml`

**Step 1: Create workflow file**

Create `.github/workflows/version-bump.yml`:

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
    # Only run if PR was merged or on direct push
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
          token: ${{ secrets.GITHUB_TOKEN }}

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

**Step 2: Commit workflow**

```bash
git add .github/workflows/version-bump.yml
git commit -m "feat: add GitHub Actions workflow for version bumping

Configure workflow to run on PR merge and direct push to main.
Uses uv to run the version bump script with Anthropic API access.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 10: Generate Lockfile

**Files:**
- Create: `scripts/bump-versions.py.lock`

**Step 1: Generate lockfile with uv**

Run:
```bash
uv lock --script scripts/bump-versions.py
```

Expected: Creates `scripts/bump-versions.py.lock`

**Step 2: Verify lockfile exists**

Run:
```bash
ls -lh scripts/bump-versions.py.lock
```

Expected: Shows lockfile with reasonable size (a few KB)

**Step 3: Commit lockfile**

```bash
git add scripts/bump-versions.py.lock
git commit -m "chore: add lockfile for version bump script

Lock dependencies for supply chain security using uv's PEP 723
lockfile support.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 11: Add Documentation

**Files:**
- Create: `scripts/README.md`

**Step 1: Create README**

Create `scripts/README.md`:

```markdown
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
```

**Step 2: Commit README**

```bash
git add scripts/README.md
git commit -m "docs: add README for version bump script

Document how the version bumping script works, how to use it,
and how to manage dependencies.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

---

## Task 12: Final Testing

**Step 1: Test script locally with dry run**

Make a small change to a plugin (e.g., add a comment to a skill):

```bash
echo "# Test change" >> opinionated-setup/skills/setup-python-project/SKILL.md
git add opinionated-setup/skills/setup-python-project/SKILL.md
git commit -m "test: trigger version bump"
```

Run script:
```bash
ANTHROPIC_API_KEY=<your-key> ./scripts/bump-versions.py
```

Expected: Detects change, analyzes with Claude, creates version bump commit

**Step 2: Push to test branch**

```bash
git push origin feature/auto-version-bump
```

**Step 3: Create PR and observe**

Create PR to main. Once merged, observe the workflow running in Actions tab.

**Step 4: Verify version bump commit**

After workflow completes, check that a version bump commit was pushed to main.

---

## Completion Checklist

- [ ] Script structure created with PEP 723 metadata
- [ ] Plugin discovery implemented
- [ ] Last version bump detection working
- [ ] Change detection implemented
- [ ] Version bumping logic tested
- [ ] Claude API integration working
- [ ] JSON file updates functional
- [ ] Commit and push logic handles race conditions
- [ ] GitHub Actions workflow configured
- [ ] Lockfile generated
- [ ] Documentation added
- [ ] End-to-end testing completed
