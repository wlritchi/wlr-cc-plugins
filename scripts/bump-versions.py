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


if __name__ == "__main__":
    sys.exit(main())
