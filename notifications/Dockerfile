# syntax=docker/dockerfile:1
#
# Image for the notifications daemon — the persistent WebSocket server half of the
# `notifications` Claude Code plugin (GitHub PR monitoring + agent messaging). The daemon
# is a PEP723 uv script; its deps are baked from the committed lock
# (daemon/notifications-daemon.py.lock) so the running pod never reaches PyPI.
#
# Build context is the plugin's `notifications/` directory, e.g.:
#   DOCKER_BUILDKIT=1 docker build \
#     -t i.wlritchi.com/wlritchi/notifications-daemon:<tag> \
#     --build-arg VERSION=<plugin-version> \
#     --build-arg VCS_REF="$(git rev-parse --short HEAD)" \
#     --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
#     notifications/

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /build
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
# Resolve the script's pinned + hashed deps from its committed lock and bake them into an
# explicit venv (no project install — the daemon is a plain script, not a uv project).
# --frozen fails the build if the lock is missing/stale, keeping the image reproducible.
COPY daemon/notifications-daemon.py daemon/notifications-daemon.py.lock ./daemon/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --script daemon/notifications-daemon.py --frozen -o requirements.txt && \
    uv venv /opt/venv && \
    uv pip install --python /opt/venv -r requirements.txt

FROM python:3.12-slim-bookworm AS runtime
ARG VERSION=0.0.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="notifications-daemon" \
      org.opencontainers.image.description="Persistent WS daemon for the notifications Claude Code plugin (PR monitoring + agent messaging)" \
      org.opencontainers.image.source="https://github.com/wlritchi/wlr-cc-plugins" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"
RUN export DEBIAN_FRONTEND=noninteractive && \
    apt-get update && \
    apt-get install -y --no-install-recommends tini && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
# Fixed uid/gid so a k8s PVC can be made writable via securityContext.fsGroup: 10001.
RUN groupadd -g 10001 app && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin app
COPY --from=build --chown=app:app /opt/venv /opt/venv
# Preserve the daemon/lib sibling layout — the daemon adds ../lib to sys.path at runtime.
COPY --chown=app:app daemon/notifications-daemon.py /app/notifications/daemon/notifications-daemon.py
COPY --chown=app:app lib/ /app/notifications/lib/
# /data is the NOTIFICATIONS_DATA_DIR mount point; created + owned here for non-PVC runs.
# In k8s the mounted PVC needs securityContext.fsGroup: 10001 to be writable by `app`.
RUN install -d -o app -g app /data
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    NOTIFICATIONS_WS_HOST=0.0.0.0 \
    NOTIFICATIONS_WS_PORT=8137 \
    NOTIFICATIONS_DATA_DIR=/data
USER app
EXPOSE 8137
VOLUME ["/data"]
# Plain `python`: deps live in the venv, so the PEP723 header is inert (no runtime uv resolve).
ENTRYPOINT ["tini", "--"]
CMD ["python", "/app/notifications/daemon/notifications-daemon.py"]
