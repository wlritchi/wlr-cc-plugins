---
name: setup-python-project
description: Use when creating a new Python project, setting up a Python package from scratch, or bootstrapping a modern Python development environment with uv and hatchling
---

# Setup Python Project

Generate a complete, production-ready project bootstrap for a modern Python project. This skill creates all necessary files for a fully functional Python package with testing, linting, and type checking configured.

## Files Created

This skill will generate:

1. **pyproject.toml** - Project metadata, dependencies, and tool configuration
2. **README.md** - Minimal README (required by hatchling)
3. **.pre-commit-config.yaml** - Pre-commit hooks with yamllint-compliant formatting
4. **src/{package}/__init__.py** - Package structure with version string
5. **tests/test_basic.py** - Basic smoke test
6. **scripts/run-pytest.sh** - Wrapper script for pytest (avoids YAML line-length issues)
7. **.git-hook-template** - Git hook wrapper that runs pre-commit via uv
8. **install-hooks.sh** - Script to install git hooks
9. **.coveragerc** (optional) - Coverage configuration
10. **[tool.pyright] in pyproject.toml** (optional) - Pyright type checker configuration

## User Prompts

When you run this skill, ask the user:
- **Project name** (e.g., exampleproj)
- **Author name and email**
- **Python minimum version** (default: 3.12)
- **Tools to include** - Offer checkboxes for: ruff, mypy, pytest, pre-commit, pyright, pytest-cov
- **Include coverage enforcement?** (yes/no) - If yes, create .coveragerc

**Note**: This skill uses uv and hatchling as the standard toolchain.

## Template: pyproject.toml

**IMPORTANT**: Always create this file with src layout and explicit wheel target configuration.

```toml
[build-system]
requires = ["hatchling", "hatch-vcs"]
build-backend = "hatchling.build"

[project]
name = "{PROJECT_NAME}"
description = "Add project description here"
readme = "README.md"
authors = [{name = "{AUTHOR_NAME}", email = "{AUTHOR_EMAIL}"}]
requires-python = ">={PYTHON_VERSION}"
dynamic = ["version"]

[project.optional-dependencies]
dev = [
    # Add selected tools here based on user choices
    # Example: "ruff", "mypy", "pytest", "pytest-cov", "pyright"
]

[tool.hatch.version]
source = "vcs"

# CRITICAL: This section is required for hatchling to find src layout packages
[tool.hatch.build.targets.wheel]
packages = ["src/{PACKAGE_NAME}"]

[tool.ruff]
line-length = 88
target-version = "py{PYTHON_VERSION_SHORT}"  # e.g., py312

[tool.ruff.format]
quote-style = "preserve"

[tool.ruff.lint]
select = ["E", "F", "I", "W", "B", "C4", "RUF"]
ignore = ["E501"]  # Line length handled by formatter

[tool.mypy]
python_version = "{PYTHON_VERSION}"
strict = true
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = [
    "--import-mode=importlib",
    "--strict-markers",
]
```


## Template: .pre-commit-config.yaml

**IMPORTANT**: Must start with `---` for yamllint compliance. Use wrapper scripts for complex commands to avoid line-length violations.

```yaml
---
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - id: end-of-file-fixer
      - id: trailing-whitespace
      - id: check-yaml
      - id: check-toml
      - id: check-json
      - id: check-merge-conflict

  - repo: https://github.com/adrienverge/yamllint
    rev: v1.35.1
    hooks:
      - id: yamllint

  - repo: https://github.com/codespell-project/codespell
    rev: v2.3.0
    hooks:
      - id: codespell
        args: [--quiet-level=2]

  - repo: local
    hooks:
      - id: pytest
        name: pytest
        # Use wrapper script to keep line length under 80 chars
        entry: bash scripts/run-pytest.sh
        language: system
        pass_filenames: false
```

## Template: scripts/run-pytest.sh

**IMPORTANT**: Make this file executable (chmod +x).

```bash
#!/usr/bin/env bash
set -e
exec uv run pytest "$@"
```

## Template: .git-hook-template

**IMPORTANT**: This template is installed into .git/hooks/ by install-hooks.sh. It allows pre-commit to run via uv without a global install.

```bash
#!/usr/bin/env bash
# Git hook wrapper that runs pre-commit via uv
# This allows pre-commit to be managed by uv instead of requiring global install

HOOK_TYPE="$(basename "$0")"
HOOK_DIR="$(dirname "$0")"

exec uv run pre-commit hook-impl \
    --config .pre-commit-config.yaml \
    --hook-type "$HOOK_TYPE" \
    --hook-dir "$HOOK_DIR" -- "$@"
```

## Template: install-hooks.sh

**IMPORTANT**: Make executable (chmod +x).

```bash
#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GIT_DIR="$SCRIPT_DIR/.git"

if [ ! -d "$GIT_DIR" ]; then
    echo "Error: .git directory not found. Run this from the repository root."
    exit 1
fi

echo "Installing git hooks..."

# Copy hook template to all relevant hook types
for hook in pre-commit pre-push post-checkout; do
    cp "$SCRIPT_DIR/.git-hook-template" "$GIT_DIR/hooks/$hook"
    chmod +x "$GIT_DIR/hooks/$hook"
    echo "  ✓ Installed $hook"
done

echo "Git hooks installed successfully!"
echo ""
echo "To test the hooks, run:"
echo "  git commit --allow-empty -m 'test commit'"
```


## Template: README.md

**IMPORTANT**: Always create this file - hatchling build will fail without it.

```markdown
# {PROJECT_NAME}

{PROJECT_DESCRIPTION}

## Installation

```bash
pip install -e .[dev]
```

## Development

See setup instructions below.
```

## Template: src/{PACKAGE_NAME}/__init__.py

**IMPORTANT**: Create this file to establish the package structure. Use underscores for package name (e.g., my_project not my-project). Version is derived from VCS tags via hatch-vcs.

```python
"""
{PROJECT_NAME} - {PROJECT_DESCRIPTION}
"""
```

## Template: tests/test_basic.py

**IMPORTANT**: Always create this file so pytest has something to run.

```python
"""Basic smoke tests."""


def test_placeholder():
    """TODO: Replace with real tests."""
    pass
```

## Template: .coveragerc (optional)

**IMPORTANT**: Only create if user requested coverage enforcement.

```ini
[run]
source = src/
omit =
    tests/*
    */__pycache__/*
    */site-packages/*

[report]
exclude_lines =
    pragma: no cover
    def __repr__
    raise AssertionError
    raise NotImplementedError
    if __name__ == .__main__.:
    if TYPE_CHECKING:
    @abstractmethod

[html]
directory = htmlcov
```

## Template: [tool.pyright] section (optional)

**IMPORTANT**: Only add to pyproject.toml if user selected pyright.

```toml
[tool.pyright]
include = ["src"]
exclude = [
    "**/__pycache__",
    "**/node_modules",
    ".venv",
    "venv",
]
pythonVersion = "{PYTHON_VERSION}"
typeCheckingMode = "basic"
reportMissingImports = true
reportMissingTypeStubs = false
```

## Setup Instructions

After creating all files, provide these instructions to the user:

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment and install dependencies
uv venv
source .venv/bin/activate  # or `.venv\Scripts\activate` on Windows
uv pip install -e .[dev]

# Install git hooks
./install-hooks.sh

# Verify setup
uv run pytest
uv run pre-commit run --all-files
```

## Verification Steps

After setup, verify everything works:

```bash
# Run tests
uv run pytest

# Run linters
uv run pre-commit run --all-files

# Test git hooks
git add .
git commit --allow-empty -m "test: verify hooks work"

# Build the package
uv run python -m build
```

## CI Integration (GitHub Actions)

```yaml
name: CI
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["{PYTHON_VERSION}", "3.12", "3.13"]

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v4

      - name: Set up Python ${{ matrix.python-version }}
        run: uv python install ${{ matrix.python-version }}

      - name: Install dependencies
        run: uv pip install -e .[dev]

      - name: Run pre-commit
        run: uv run pre-commit run --all-files

      - name: Run tests with coverage
        run: |
          uv run pytest --cov={PACKAGE_NAME} --cov-report=xml --cov-report=term

      - name: Upload coverage
        uses: codecov/codecov-action@v4
        if: matrix.python-version == '{PYTHON_VERSION}'
```

## Execution Instructions

When executing this skill:

1. **Ask the user** for all required information (project name, author, python version, tools, etc.)
2. **Create all files** using the Write tool - substitute placeholders with actual values
3. **Make scripts executable**: Run `chmod +x scripts/run-pytest.sh` and `chmod +x install-hooks.sh`
4. **Initialize git** if not already a repo: `git init`
5. **Provide the setup instructions**
6. **Offer to run the setup** commands if the user wants

### Placeholder Reference

- `{PROJECT_NAME}` - User's project name (with hyphens, e.g., "my-project")
- `{PACKAGE_NAME}` - Python package name (with underscores, e.g., "my_project")
- `{AUTHOR_NAME}` - User's full name
- `{AUTHOR_EMAIL}` - User's email address
- `{PYTHON_VERSION}` - Minimum Python version (e.g., "3.12")
- `{PYTHON_VERSION_SHORT}` - Short version for ruff (e.g., "py312")
- `{PROJECT_DESCRIPTION}` - Short project description
