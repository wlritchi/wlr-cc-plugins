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
import re
import sys
from pathlib import Path
from typing import Optional

import anthropic
import git


def load_marketplace_config() -> dict:
    """Load marketplace.json to get list of plugins."""
    marketplace_path = Path(".claude-plugin/marketplace.json")

    if not marketplace_path.exists():
        print(f"❌ Error: {marketplace_path} not found")
        sys.exit(1)

    try:
        with open(marketplace_path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Error: Invalid JSON in {marketplace_path}: {e}")
        sys.exit(1)


def get_plugins() -> list[dict]:
    """Get list of plugins from marketplace config."""
    config = load_marketplace_config()
    plugins = config.get("plugins", [])

    print(f"📦 Found {len(plugins)} plugins:")
    for plugin in plugins:
        name = plugin.get('name', 'unknown')
        version = plugin.get('version', 'unknown')
        if name == 'unknown' or version == 'unknown':
            print(f"  ⚠️  Plugin missing name or version: {plugin}")
            continue
        print(f"  - {name} (v{version})")

    return plugins


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
                    if diff.diff:
                        # Match actual JSON version field changes like: "version": "0.1.0"
                        # This requires the pattern to appear on changed lines (+ or - in diff)
                        version_pattern = rb'[+-].*"version"\s*:\s*"[0-9]+\.[0-9]+\.[0-9]+"'
                        if re.search(version_pattern, diff.diff):
                            return commit.hexsha
        except (IndexError, git.GitCommandError):
            continue

    return None


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

    return 0


if __name__ == "__main__":
    sys.exit(main())
