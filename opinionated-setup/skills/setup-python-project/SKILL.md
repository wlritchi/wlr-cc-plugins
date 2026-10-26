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
3. **.pre-commit-config.yaml** - Git hooks, run by [prek](https://github.com/j178/prek), with yamllint-compliant formatting
4. **src/{package}/__init__.py** - Package structure with version string
5. **tests/test_basic.py** - Basic smoke test
6. **scripts/run-pytest.sh** - Wrapper script for pytest (avoids YAML line-length issues)
7. **.git-hook-template** - Git hook wrapper that runs prek via uv
8. **install-hooks.sh** - Script to install git hooks
9. **.gitignore** - Ignores bytecode, the venv, build output, and tool caches
10. **.coveragerc** (optional) - Coverage configuration
11. **[tool.pyright] in pyproject.toml** (optional) - Pyright type checker configuration

## User Prompts

When you run this skill, ask the user:
- **Project name** (e.g., exampleproj)
- **Author name and email**
- **Python minimum version** (default: 3.12)
- **Tools to include** - Offer checkboxes for: ruff, ty, pytest, prek, pyright, pytest-cov
- **Include coverage enforcement?** (yes/no) - If yes, create .coveragerc

**Note**: This skill uses uv and hatchling as the standard toolchain. prek is a faster drop-in replacement for pre-commit; it reads the same `.pre-commit-config.yaml`. ty is Astral's type checker, used in place of mypy.

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

# Use dependency-groups (PEP 735) for dev dependencies - native to uv
[dependency-groups]
dev = [
    # Add selected tools here based on user choices
    # Example: "ruff", "ty", "prek", "pytest", "pytest-cov", "pyright"
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
select = [
    "E",      # pycodestyle errors
    "F",      # pyflakes
    "I",      # isort
    "W",      # pycodestyle warnings
    "B",      # flake8-bugbear
    "C4",     # flake8-comprehensions
    "N",      # pep8-naming
    "S",      # flake8-bandit (security)
    "ANN",    # flake8-annotations
    "ASYNC",  # flake8-async
    "UP",     # pyupgrade
    "RUF",    # ruff-specific rules
    "T201",   # print statements
]
ignore = [
    "E501",   # Line length handled by formatter
    "S101",   # Allow assert in tests
]

[tool.ty.environment]
python-version = "{PYTHON_VERSION}"

# Opt-in rules that are off by default, for stricter checking
[tool.ty.rules]
possibly-unresolved-reference = "error"
possibly-missing-attribute = "error"
possibly-missing-import = "error"
missing-type-argument = "error"
blanket-ignore-comment = "error"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = [
    "--import-mode=importlib",
    "--strict-markers",
]
```


## Template: .pre-commit-config.yaml

**IMPORTANT**: Must start with `---` for yamllint compliance. Use wrapper scripts for complex commands to avoid line-length violations.

prek reads this file unchanged, so the config also stays usable by upstream pre-commit. prek automatically runs its Rust-native implementations of the `pre-commit-hooks` hooks below. Do not change that repo to `repo: builtin`, which upstream pre-commit cannot read.

Omit the `ty` hook if the user did not select ty.

**VERSION NOTE**: Before creating this file, check for the latest versions of uv-pre-commit, ruff-pre-commit, ty-pre-commit, pre-commit-hooks, yamllint, and codespell. The versions below are examples and may be outdated. After creating the file, `uv run prek update` bumps every `rev` to the latest tag.

```yaml
---
default_install_hook_types: [pre-commit, post-checkout]
default_stages: [pre-commit, pre-push, manual]
repos:
  # Keep lockfile in sync
  - repo: https://github.com/astral-sh/uv-pre-commit
    rev: 0.13.5  # CHECK FOR LATEST VERSION
    hooks:
      - id: uv-lock
        require_serial: true

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.17.2  # CHECK FOR LATEST VERSION
    hooks:
      - id: ruff-check
        args: [--fix]
      - id: ruff-format

  # Type-checks the whole project, not only staged files
  - repo: https://github.com/astral-sh/ty-pre-commit
    rev: v0.0.92  # CHECK FOR LATEST VERSION
    hooks:
      - id: ty

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0  # CHECK FOR LATEST VERSION
    hooks:
      - id: end-of-file-fixer
        exclude: '\.md$'
      - id: trailing-whitespace
        exclude: '(\.md|\.patch)$'
      - id: check-yaml
      - id: check-toml
      - id: check-json
      - id: check-merge-conflict
      - id: check-case-conflict
      - id: check-executables-have-shebangs
      - id: mixed-line-ending

  - repo: https://github.com/adrienverge/yamllint
    rev: v1.38.0  # CHECK FOR LATEST VERSION
    hooks:
      - id: yamllint

  - repo: https://github.com/codespell-project/codespell
    rev: v2.4.3  # CHECK FOR LATEST VERSION
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

Do not add a `ci:` section for pre-commit.ci. The ty hook needs network access, which pre-commit.ci blocks. The GitHub Actions workflow below runs prek instead.

## Template: scripts/run-pytest.sh

**IMPORTANT**: Make this file executable (chmod +x).

```bash
#!/usr/bin/env bash
set -e
exec uv run pytest "$@"
```

## Template: .git-hook-template

**IMPORTANT**: This template is installed into .git/hooks/ by install-hooks.sh. It allows prek to run via uv without a global install. Use it instead of `prek install`: that shim stores the absolute path of the prek binary, so it runs the main checkout's venv from every git worktree.

```bash
#!/usr/bin/env bash
# Git hook wrapper that runs prek via uv
# This allows prek to be managed by uv instead of requiring global install

HOOK_TYPE="$(basename "$0")"
HOOK_DIR="$(dirname "$0")"

exec uv run prek hook-impl \
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
    echo "  Installed $hook"
done

echo "Git hooks installed successfully!"
echo ""
echo "To test the hooks, run:"
echo "  git commit --allow-empty -m 'test commit'"
```

## Template: .gitignore

**IMPORTANT**: Always create this file before the first `git add`. The setup steps run pytest first, which creates `__pycache__/` directories that would otherwise be committed.

```gitignore
# Bytecode
__pycache__/
*.py[cod]

# Virtual environment
.venv/

# Build output
build/
dist/
*.egg-info/

# Tool caches
.pytest_cache/
.ruff_cache/

# Coverage
.coverage
.coverage.*
coverage.xml
htmlcov/
```

## Template: README.md

**IMPORTANT**: Always create this file - hatchling build will fail without it.

```markdown
# {PROJECT_NAME}

{PROJECT_DESCRIPTION}

## Installation

```bash
uv sync
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


def test_placeholder() -> None:
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
reportImplicitOverride = true
```

## Setup Instructions

After creating all files, provide these instructions to the user:

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment and install dependencies
uv venv
source .venv/bin/activate  # or `.venv\Scripts\activate` on Windows
uv sync --group dev

# Install git hooks
./install-hooks.sh

# Verify setup
uv run pytest
uv run prek run --all-files
```

## Verification Steps

After setup, verify everything works:

```bash
# Run tests
uv run pytest

# Run linters and the type checker
uv run prek run --all-files

# Run the type checker alone
uv run ty check

# Test git hooks
git add .
git commit --allow-empty -m "test: verify hooks work"

# Build the package
uv build
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
        # List every minor version from {PYTHON_VERSION} through the latest
        # release. This example assumes a minimum of 3.12.
        python-version: ["3.12", "3.13", "3.14", "3.15"]

    steps:
      - uses: actions/checkout@v7

      - name: Install uv
        uses: astral-sh/setup-uv@v10

      - name: Set up Python ${{ matrix.python-version }}
        run: uv python install ${{ matrix.python-version }}

      - name: Install dependencies
        run: uv sync --group dev

      - name: Run prek
        run: uv run prek run --all-files

      - name: Run tests with coverage
        run: |
          uv run pytest --cov={PACKAGE_NAME} --cov-report=xml --cov-report=term

      - name: Upload coverage
        uses: codecov/codecov-action@v7
        if: matrix.python-version == '{PYTHON_VERSION}'
```

## Execution Instructions

When executing this skill:

1. **Ask the user** for all required information (project name, author, python version, tools, etc.)
2. **Check for latest versions** of hook repos (uv-pre-commit, ruff-pre-commit, ty-pre-commit, etc.)
3. **Create all files** using the Write tool - substitute placeholders with actual values
4. **Make scripts executable**: Run `chmod +x scripts/run-pytest.sh` and `chmod +x install-hooks.sh`
5. **Initialize git** if not already a repo: `git init`
6. **Provide the setup instructions**
7. **Offer to run the setup** commands if the user wants

### Placeholder Reference

- `{PROJECT_NAME}` - User's project name (with hyphens, e.g., "my-project")
- `{PACKAGE_NAME}` - Python package name (with underscores, e.g., "my_project")
- `{AUTHOR_NAME}` - User's full name
- `{AUTHOR_EMAIL}` - User's email address
- `{PYTHON_VERSION}` - Minimum Python version (e.g., "3.12")
- `{PYTHON_VERSION_SHORT}` - Short version for ruff (e.g., "py312")
- `{PROJECT_DESCRIPTION}` - Short project description

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
   - This skill's identifier (`opinionated-setup:setup-python-project`)
   - Summary of feedback/preferences
   - Relevant conversation context showing the corrections
4. Report the PR number to the user when the sub-agent completes
