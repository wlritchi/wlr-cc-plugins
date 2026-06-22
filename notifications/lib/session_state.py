# vim: filetype=python
"""Shared session-id correlation between the notifications MCP server and the
SessionStart hook.

Background: when a session is resumed (`/resume`, or `--resume` with the picker),
the CLAUDE_CODE_SESSION_ID handed to the already-spawned stdio MCP server is the
*temporary* id from before the user picked a session. There is no in-band way for
Claude Code to tell the running server the id changed.

The SessionStart hook *does* receive the real session id. It and the server share
exactly one correlatable thing: the Claude Code process they both descend from.
That pid is not exposed as an env var (the server's getppid() is `uv`, with
Claude Code as the grandparent), so each side discovers it by walking its own
parent chain until it finds the Claude Code process. The hook then writes the
session id to a per-instance state file keyed by that pid, and the server reads
it on demand, falling back to the env var.

stdlib only, so the hook can run without building a dependency environment.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

SESSION_ID_ENV_VAR = "CLAUDE_CODE_SESSION_ID"

_MAX_ANCESTOR_DEPTH = 16


def _read_proc(pid: int, name: str) -> bytes | None:
    """Read /proc/<pid>/<name>, or None if unavailable (non-Linux, gone, denied)."""
    try:
        return Path("/proc", str(pid), name).read_bytes()
    except OSError:
        return None


def _parent_pid(pid: int) -> int | None:
    """Return the parent pid of `pid`, via /proc with a `ps` fallback."""
    status = _read_proc(pid, "status")
    if status is not None:
        for line in status.decode(errors="replace").splitlines():
            if line.startswith("PPid:"):
                try:
                    return int(line.split()[1])
                except (IndexError, ValueError):
                    return None
        return None
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        return int(out.stdout.strip())
    except (OSError, ValueError):
        return None


def _looks_like_claude(pid: int) -> bool:
    """Best-effort: does `pid` look like the Claude Code CLI process?"""
    comm = _read_proc(pid, "comm")
    if comm is not None:
        if comm.decode(errors="replace").strip() == "claude":
            return True
        cmdline = _read_proc(pid, "cmdline")
        argv = cmdline.split(b"\x00") if cmdline else []
    else:
        try:
            res = subprocess.run(
                ["ps", "-o", "args=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return False
        argv = [part.encode() for part in res.stdout.split()]

    if not argv or not argv[0]:
        return False
    argv0 = os.path.basename(argv[0].decode(errors="replace"))
    joined = b" ".join(argv).decode(errors="replace")
    return argv0 == "claude" or "claude-code" in joined or "claude/cli" in joined


def resolve_claude_pid(start: int | None = None) -> int | None:
    """Walk up from `start` (default: this process) to the nearest Claude Code
    ancestor pid, or None if it can't be found (feature then degrades to no-op)."""
    cur = _parent_pid(os.getpid() if start is None else start)
    seen: set[int] = set()
    for _ in range(_MAX_ANCESTOR_DEPTH):
        if cur is None or cur <= 1 or cur in seen:
            return None
        if _looks_like_claude(cur):
            return cur
        seen.add(cur)
        cur = _parent_pid(cur)
    return None


def state_dir() -> Path:
    """Per-user directory holding the session-id state files."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg):
        return Path(xdg) / "claude-notifications"
    suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    return Path(tempfile.gettempdir()) / f"claude-notifications{suffix}"


def _state_path(claude_pid: int) -> Path:
    return state_dir() / f"{claude_pid}.json"


def write_session(claude_pid: int, session_id: str, source: str | None = None) -> Path:
    """Atomically record `session_id` for the given Claude Code pid."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass

    path = _state_path(claude_pid)
    payload: dict[str, object] = {"session_id": session_id, "claude_pid": claude_pid}
    if source:
        payload["source"] = source

    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)
    return path


def read_session(claude_pid: int) -> str | None:
    """Read the recorded session id for the given Claude Code pid, if any."""
    try:
        data = json.loads(_state_path(claude_pid).read_text())
    except (OSError, ValueError):
        return None
    session_id = data.get("session_id")
    return session_id or None


def effective_session_id() -> tuple[str | None, str]:
    """Resolve the best-known session id and a human-readable source label.

    State file (keyed by the Claude Code pid) wins over the env var, because on a
    resumed session the env var holds the stale temporary id.
    """
    claude_pid = resolve_claude_pid()
    if claude_pid is not None:
        session_id = read_session(claude_pid)
        if session_id:
            return session_id, f"state file (claude pid {claude_pid})"

    env = os.environ.get(SESSION_ID_ENV_VAR)
    if env:
        return env, f"env ({SESSION_ID_ENV_VAR})"
    return None, "unavailable"


def _pid_alive(pid: int) -> bool:
    if Path("/proc", str(pid)).exists():
        return True
    if _read_proc(pid, "comm") is not None:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reap_stale() -> None:
    """Best-effort cleanup of state files for Claude Code pids that have exited."""
    directory = state_dir()
    if not directory.is_dir():
        return
    for entry in directory.glob("*.json"):
        try:
            pid = int(entry.stem)
        except ValueError:
            continue
        if not _pid_alive(pid):
            try:
                entry.unlink()
            except OSError:
                pass
