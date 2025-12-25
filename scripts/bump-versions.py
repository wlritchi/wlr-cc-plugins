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

    # Find last version bump for each plugin
    print("\n🔎 Finding last version bumps...")
    for plugin in plugins:
        last_bump = find_last_version_bump(repo, plugin["name"])
        if last_bump:
            print(f"  - {plugin['name']}: {last_bump[:8]}")
        else:
            print(f"  - {plugin['name']}: (no version bump found, using repo root)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
