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
