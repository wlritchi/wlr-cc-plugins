---
name: setup-python-dockerfile
description: Use when generating a Dockerfile for a Python project using uv, or when containerizing a uv-based Python application
---

# Setup Python Dockerfile

Generate a ready-to-build Dockerfile using uv for dependency management. This skill produces multi-stage Dockerfile templates (minimal and production variants) with detailed explanations. The generated Dockerfile is self-contained and only references the project's `pyproject.toml` and `uv.lock` which should already exist in the repository.

## User Prompts

When you run this skill, ask the user:
- **Entrypoint** - Module path for the application (e.g., `your_package.entrypoint` or `-m module`)
- **Runtime system deps** - Comma-separated list of apt packages needed at runtime (e.g., `libpq-dev, build-essential`), or "none"
- **Template** - Which template to use: minimal or production

## Template: Minimal Dockerfile

Use this template for simpler deployments that don't need OCI labels or versioning metadata.

```dockerfile
# Minimal multi-stage Dockerfile using uv
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ARG VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION} UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Use cache mount for uv and bind pyproject + uv.lock for reproducible installs
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev
COPY . /app
# Re-run uv sync to ensure project and extras are installed into .venv
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
# Optional: install runtime system deps
RUN export DEBIAN_FRONTEND=noninteractive && \
    apt-get update && \
    apt-get install -y --no-install-recommends tini {RUNTIME_APT} && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
# Create non-root user
RUN useradd --create-home --shell /bin/bash app
USER app
COPY --from=build --chown=app:app /app/.venv /app/.venv
COPY --from=build --chown=app:app /app/src /app/src
WORKDIR /app/src
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "{ENTRYPOINT}"]
```

## Template: Production Dockerfile

Use this template for production deployments that need OCI labels, versioning, and support for extras.

```dockerfile
# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ARG VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION} UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Mount uv cache and bind pyproject/lock to make deterministic build
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev
# Copy everything and sync again to pick up project sources and extras
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --extra server

FROM python:3.12-slim-bookworm AS runtime
ARG VERSION
ARG VCS_REF
ARG BUILD_DATE
LABEL org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.created="$BUILD_DATE"
RUN export DEBIAN_FRONTEND=noninteractive && \
    apt-get update && \
    apt-get install -y --no-install-recommends tini {RUNTIME_APT} && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --shell /bin/bash app
USER app
WORKDIR /home/app
ENV PATH="/home/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=build --chown=app:app /app/.venv /home/app/.venv
COPY --from=build --chown=app:app /app/src /home/app/src
ENTRYPOINT ["tini", "--"]
CMD ["/home/app/.venv/bin/python", "-m", "{ENTRYPOINT}"]
```

## Build Notes

- **Enable BuildKit**: Set `DOCKER_BUILDKIT=1` when building to allow cache mounts
- **Deterministic runtime**: Copy `.venv` from build stage instead of running pip installs at runtime
- **Process management**: Use `tini` or `dumb-init` as ENTRYPOINT to handle signals and avoid orphaned processes (see Init Systems section below)
- **Native libraries**: If you need helper scripts to install native libs, generate them in the build stage and COPY them into runtime
- **DEBIAN_FRONTEND**: Always set `DEBIAN_FRONTEND=noninteractive` in apt-get RUN commands to prevent interactive prompts from hanging builds
- **Security hardening** (optional): Add `apt-get -y upgrade` before installing packages to apply security patches to base image packages. Tradeoff: less reproducible builds, larger layers, longer build times
- **ARG propagation**: Build args don't propagate between stages automatically—redeclare them in each stage where needed (e.g., `ARG VERSION` must be declared in both build and runtime stages to use in labels)

### Init Systems

Both `tini` and `dumb-init` handle proper PID 1 behavior (reaping zombies, signal forwarding):

| Init System | Package | Notes |
|-------------|---------|-------|
| tini | `tini` | Lightweight, widely used, single binary |
| dumb-init | `dumb-init` | From Yelp, similar functionality |

```dockerfile
# tini
ENTRYPOINT ["tini", "--"]

# dumb-init (--single-child for similar behavior to tini)
ENTRYPOINT ["/usr/bin/dumb-init", "--single-child", "--"]
```

## Advanced Patterns

### External Scripts for Complex Setups

For projects with complex runtime setup needs (GPG key imports, conditional dependencies, config file handling), use external shell scripts instead of inline RUN commands:

```dockerfile
# Build stage copies scripts
COPY docker/ /app/docker/

# Runtime stage uses scripts
COPY --from=build /app/docker /app/docker
RUN /app/docker/deps-runtime.sh
```

**Example `deps-runtime.sh`:**
```bash
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y install --no-install-recommends \
    your-package-1 \
    your-package-2
apt-get clean
rm -rf /var/lib/apt/lists/*
```

**Benefits:** Easier to maintain, better readability for long package lists, can be versioned and tested independently, enables conditional logic that's awkward in Dockerfile RUN.

### Entrypoint Script Pattern

For runtime initialization (config copying, secret handling, working directory setup), use an entrypoint script:

```bash
#!/bin/bash
set -euo pipefail

# Optional: handle working directory
if [ -n "${WORKING_DIR:-}" ]; then
    cd "$WORKING_DIR"
fi

# Optional: copy config from mounted volumes
if [ -d /config ] && [ -f /config/app.conf ]; then
    cp /config/app.conf ~/.config/app/
fi

# Execute command or default
if [ "$#" -gt 0 ]; then
    exec "$@"
else
    exec python -m your_package.entrypoint
fi
```

**Dockerfile usage:**
```dockerfile
COPY --from=build --chown=app:app /app/docker/entrypoint.sh /app/entrypoint.sh
ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
CMD ["python", "-m", "your_package.entrypoint"]
```

### HEALTHCHECK (Optional)

For production deployments, add health checks:

```dockerfile
# Basic Python import check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import your_package" || exit 1

# For web services
HEALTHCHECK --interval=30s --timeout=3s \
    CMD curl -f http://localhost:8000/health || exit 1
```

## Execution Instructions

When executing this skill:

1. **Ask the user** for entrypoint, runtime dependencies, and template choice
2. **Generate the Dockerfile** using the Write tool - substitute placeholders with actual values
3. **Explain the key features** of the generated Dockerfile

### Placeholder Reference

- `{ENTRYPOINT}` - Module path for python -m (e.g., "my_package.main")
- `{RUNTIME_APT}` - Space-separated list of apt packages, or empty string if none

### Example Build Commands

Provide these to the user after generating the Dockerfile:

```bash
# Build with BuildKit
DOCKER_BUILDKIT=1 docker build -t myapp .

# Build with version info (production template)
DOCKER_BUILDKIT=1 docker build \
  --build-arg VERSION=$(git describe --tags --always) \
  --build-arg VCS_REF=$(git rev-parse --short HEAD) \
  --build-arg BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
  -t myapp .

# Run the container
docker run --rm -it myapp
```

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
   - This skill's identifier (`opinionated-setup:setup-python-dockerfile`)
   - Summary of feedback/preferences
   - Relevant conversation context showing the corrections
4. Report the PR number to the user when the sub-agent completes
