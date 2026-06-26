# vim: filetype=python
"""Shared helpers for the end-to-end tests: process spawning, a fake GitHub
GraphQL server, and a raw MCP-over-stdio client (we read raw frames so we can see
the custom notifications/claude/channel events the high-level client would drop)."""

import contextlib
import http.server
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import anyio
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)

PLUGIN = Path(__file__).resolve().parent.parent
DAEMON = str(PLUGIN / "daemon" / "notifications-daemon.py")
RELAY = str(PLUGIN / "mcp" / "notifications-server.py")
CHANNEL_METHOD = "notifications/claude/channel"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int | str, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", int(port))) == 0:
                return True
        time.sleep(0.2)
    return False


def daemon_env(
    ws_port: int,
    data_dir: Path,
    *,
    graphql_url: str | None = None,
    poll_seconds: str = "1",
    warm_ttl: str | None = None,
) -> dict:
    env = dict(os.environ)
    env["NOTIFICATIONS_WS_PORT"] = str(ws_port)
    env["NOTIFICATIONS_DATA_DIR"] = str(data_dir)
    env["NOTIFICATIONS_PR_POLL_SECONDS"] = poll_seconds
    if warm_ttl is not None:
        env["NOTIFICATIONS_PR_WARM_TTL_SECONDS"] = warm_ttl
    if graphql_url:
        env["GITHUB_GRAPHQL_URL"] = graphql_url
        env["GITHUB_TOKEN"] = "test-token"
    return env


def relay_env(
    ws_port: int,
    data_dir: Path,
    xdg_dir: Path,
    session_id: str,
    *,
    cache_dir: Path | None = None,
    project_dir: str | None = None,
    debounce_seconds: str = "0.5",
) -> dict:
    env = dict(os.environ)
    env["NOTIFICATIONS_WS_PORT"] = str(ws_port)
    env["NOTIFICATIONS_DATA_DIR"] = str(data_dir)
    env["XDG_RUNTIME_DIR"] = str(xdg_dir)  # isolate the session-id state file lookup
    env["CLAUDE_CODE_SESSION_ID"] = session_id
    # Small push-mode debounce window: a same-pass burst still coalesces (items
    # arrive within ms), but a lone notification is delayed only briefly so tests
    # stay within their timeouts.
    env["NOTIFICATIONS_DEBOUNCE_SECONDS"] = debounce_seconds
    if cache_dir is not None:
        env["XDG_CACHE_HOME"] = str(
            cache_dir
        )  # where the relay looks for the channel log
        # The explicit override is what makes the seeded-log lookup
        # platform-independent, so the e2e suite can pass on macOS, where
        # _cache_root() would otherwise resolve to ~/Library/Caches and miss the seed.
        env["NOTIFICATIONS_MCP_LOG_CACHE_DIR"] = str(cache_dir)
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = project_dir
    return env


_FAKE_PROJECT = "/test/proj"


def seed_channel_log(
    cache_dir: Path, *, registered: bool, project_dir: str = _FAKE_PROJECT
) -> None:
    """Write a fake Claude Code MCP log so the relay detects push (registered) or pull mode."""
    encoded = re.sub(r"[/.]", "-", project_dir)
    directory = (
        Path(cache_dir)
        / "claude-cli-nodejs"
        / encoded
        / "mcp-logs-plugin-notifications-notifications"
    )
    directory.mkdir(parents=True, exist_ok=True)
    marker = (
        "Channel notifications registered"
        if registered
        else "Channel notifications skipped: server not in --channels list for this session"
    )
    log = directory / "2026-01-01T00-00-00-000Z.jsonl"
    log.write_text(json.dumps({"message": marker}) + "\n")
    # The relay only trusts a channel-detection log whose mtime is within a few
    # seconds of its own startup (mirroring how Claude Code writes this log just
    # *after* spawning the MCP server). We seed it *before* spawning the relay, so
    # under heavy load a slow `uv run` boot could otherwise push the relay's detect
    # clock far enough past the seed time to make the log look stale — flipping it
    # to pull mode and dropping the pushed event the e2e tests await. Stamp the mtime
    # forward so the log stays fresh regardless of how long the relay takes to boot.
    future = time.time() + 3600
    os.utime(log, (future, future))


def push_relay_env(
    tmp_path: Path, ws_port: int, data_dir: Path, xdg_dir: Path, session_id: str
) -> dict:
    """relay_env wired for channel (push) mode via a seeded 'registered' MCP log."""
    cache = Path(tmp_path) / "cache"
    seed_channel_log(cache, registered=True)
    return relay_env(
        ws_port,
        data_dir,
        xdg_dir,
        session_id,
        cache_dir=cache,
        project_dir=_FAKE_PROJECT,
    )


@contextlib.contextmanager
def daemon_process(env: dict):
    proc = subprocess.Popen(
        ["uv", "run", "-qs", DAEMON],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_port(env["NOTIFICATIONS_WS_PORT"]):
            raise RuntimeError("daemon did not start listening")
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def relay_params(env: dict) -> StdioServerParameters:
    return StdioServerParameters(command="uv", args=["run", "-qs", RELAY], env=env)


def _slice_page(nodes: list, offset: int, page_size: int) -> dict:
    """A connection {pageInfo, nodes} for nodes[offset:offset+page_size]. Cursors are
    just stringified next-page indices, so a follow-up `after:<endCursor>` slices on."""
    chunk = nodes[offset : offset + page_size]
    nxt = offset + page_size
    has_next = nxt < len(nodes)
    return {
        "pageInfo": {
            "hasNextPage": has_next,
            "endCursor": str(nxt) if has_next else None,
        },
        "nodes": chunk,
    }


def _rollup_contexts(pr: dict) -> dict | None:
    """The head commit's check-rollup `contexts` connection on a pr node, or None."""
    commits = (pr.get("commits") or {}).get("nodes") or []
    if not commits:
        return None
    rollup = (commits[0].get("commit") or {}).get("statusCheckRollup")
    if not rollup:
        return None
    return rollup.get("contexts")


def _with_sliced_contexts(pr: dict, sliced: dict) -> dict:
    """A copy of pr['commits'] with the rollup's contexts replaced by `sliced`, without
    mutating the canonical pr node (tests mutate pr between polls)."""
    commits = dict(pr.get("commits") or {})
    nodes = [dict(n) for n in (commits.get("nodes") or [])]
    if not nodes:
        return commits
    commit = dict(nodes[0].get("commit") or {})
    rollup = dict(commit.get("statusCheckRollup") or {})
    rollup["contexts"] = sliced
    commit["statusCheckRollup"] = rollup
    nodes[0]["commit"] = commit
    commits["nodes"] = nodes
    return commits


def _target_connection(query: str) -> str | None:
    """Which paginated connection a follow-up page query targets: the one whose arg
    list carries `after` (the per-thread comments(first:100) sub-selection has no
    `after`, so it never matches)."""
    for name, args in re.findall(r"(\w+)\(([^)]*)\)", query):
        if "after" in args and name in {
            "reviews",
            "comments",
            "reviewThreads",
            "contexts",
        }:
            return name
    return None


class FakeGitHub:
    """A tiny cursor-aware GraphQL endpoint. `pr` is the pullRequest node for `number`;
    mutate it between polls. Any other number returns null (i.e. not found).

    The initial query returns each paginated connection sliced to `page_size` (default
    large, so PRs that fit one page behave exactly as before) plus a pageInfo cursor;
    a follow-up query carrying `variables.cursor` returns the next slice of whichever
    connection it targets. Cursors are stringified node offsets."""

    def __init__(
        self,
        number: int,
        pr: dict,
        *,
        page_size: int = 100,
        extra: dict[int, dict] | None = None,
    ) -> None:
        self.number = number
        self.pr = pr
        # Optional additional PR nodes keyed by number, so one fake (one GraphQL URL)
        # can serve several PRs at once — the daemon has a single GITHUB_GRAPHQL_URL,
        # so a session subscribed to two PRs needs them behind the same endpoint.
        self.extra: dict[int, dict] = dict(extra or {})
        self.page_size = page_size
        self._server: http.server.ThreadingHTTPServer | None = None
        # Optional fault injection (driven cross-thread from a test). When
        # fault_status is set, do_POST returns that HTTP status (with any extra
        # headers, e.g. Retry-After) instead of the normal 200, so tests can exercise
        # the daemon's error taxonomy (auth / rate-limited / transient). fault_count
        # None means "until cleared"; an int auto-recovers after that many requests.
        self.fault_status: int | None = None
        self.fault_headers: dict[str, str] = {}
        self.fault_count: int | None = None

    def set_fault(
        self,
        status: int,
        *,
        headers: dict[str, str] | None = None,
        count: int | None = None,
    ) -> None:
        """Make the next request(s) fail with `status` (and optional extra `headers`).
        count=None faults until clear_fault(); an int faults that many requests then
        auto-recovers."""
        self.fault_status = status
        self.fault_headers = dict(headers or {})
        self.fault_count = count

    def clear_fault(self) -> None:
        self.fault_status = None
        self.fault_headers = {}
        self.fault_count = None

    def _pr_for(self, number: object) -> dict | None:
        """The live pr node for `number` (primary or one of `extra`), or None."""
        if number == self.number:
            return self.pr
        return self.extra.get(number)  # type: ignore[arg-type]

    def _first_page(self, source: dict) -> dict:
        """The pr node with every paginated connection sliced to its first page."""
        pr = dict(source)
        for conn in ("reviews", "comments", "reviewThreads"):
            if conn in source:
                full = (source.get(conn) or {}).get("nodes") or []
                pr[conn] = _slice_page(full, 0, self.page_size)
        contexts = _rollup_contexts(source)
        if contexts is not None:
            full = contexts.get("nodes") or []
            pr["commits"] = _with_sliced_contexts(
                source, _slice_page(full, 0, self.page_size)
            )
        return pr

    def _page_response(self, query: str, offset: int, source: dict) -> dict:
        """The body for a follow-up page query: just its connection at the right path."""
        name = _target_connection(query)
        if name == "contexts":
            contexts = _rollup_contexts(source) or {}
            full = contexts.get("nodes") or []
            pr = {
                "commits": _with_sliced_contexts(
                    source, _slice_page(full, offset, self.page_size)
                )
            }
        elif name in ("reviews", "comments", "reviewThreads"):
            full = (source.get(name) or {}).get("nodes") or []
            pr = {name: _slice_page(full, offset, self.page_size)}
        else:
            pr = {}
        return {"data": {"repository": {"pullRequest": pr}}}

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def graphql_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/graphql"

    def __enter__(self) -> "FakeGitHub":
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # silence
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length) or b"{}")
                variables = req.get("variables") or {}
                number = variables.get("number")
                cursor = variables.get("cursor")
                if outer.fault_status is not None:
                    status = outer.fault_status
                    headers = dict(outer.fault_headers)
                    if outer.fault_count is not None:
                        outer.fault_count -= 1
                        if outer.fault_count <= 0:
                            outer.clear_fault()  # auto-recover after the burst
                    body = json.dumps({"message": "fault"}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                source = outer._pr_for(number)
                if source is None:
                    payload = {"data": {"repository": {"pullRequest": None}}}
                elif cursor is None:  # the main query -> first page of every connection
                    payload = {
                        "data": {
                            "repository": {"pullRequest": outer._first_page(source)}
                        }
                    }
                else:  # a follow-up query draining one connection from `cursor`
                    payload = outer._page_response(
                        req.get("query") or "", int(cursor), source
                    )
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-RateLimit-Remaining", "4999")
                self.send_header("X-RateLimit-Reset", str(int(time.time()) + 3600))
                self.end_headers()
                self.wfile.write(body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            self._server.shutdown()


# --------------------------------------------------------------------------- #
# raw MCP-over-stdio client helpers (run inside an anyio scope)
# --------------------------------------------------------------------------- #


async def mcp_send(write, obj) -> None:
    await write.send(SessionMessage(message=JSONRPCMessage(obj)))


async def mcp_await_response(read, want_id: int, timeout: float = 20.0):
    """Return (response, [channel events seen meanwhile])."""
    channels: list = []
    with anyio.move_on_after(timeout):
        async for message in read:
            root = message.message.root
            if isinstance(root, JSONRPCNotification) and root.method == CHANNEL_METHOD:
                channels.append(root)
            elif isinstance(root, JSONRPCResponse) and root.id == want_id:
                return root, channels
    return None, channels


async def mcp_await_channel(read, timeout: float = 20.0):
    with anyio.move_on_after(timeout):
        async for message in read:
            root = message.message.root
            if isinstance(root, JSONRPCNotification) and root.method == CHANNEL_METHOD:
                return root
    return None


async def mcp_await_channel_with(read, needle: str, timeout: float = 20.0):
    """Await the next channel event whose content contains `needle`, skipping others."""
    with anyio.move_on_after(timeout):
        async for message in read:
            root = message.message.root
            if isinstance(root, JSONRPCNotification) and root.method == CHANNEL_METHOD:
                if needle in (root.params.get("content") or ""):
                    return root
    return None


async def mcp_await_channel_with_kinds(read, needle: str, timeout: float = 20.0):
    """Like mcp_await_channel_with, but also return every channel meta `kind` seen while
    waiting, so a test can assert no unwanted (e.g. terminal) event slipped in first.
    Returns (event_or_None, [kinds...])."""
    kinds: list = []
    with anyio.move_on_after(timeout):
        async for message in read:
            root = message.message.root
            if isinstance(root, JSONRPCNotification) and root.method == CHANNEL_METHOD:
                kinds.append((root.params.get("meta") or {}).get("kind"))
                if needle in (root.params.get("content") or ""):
                    return root, kinds
    return None, kinds


async def mcp_collect_channels(read, kinds: set[str], timeout: float = 20.0) -> dict:
    got: dict = {}
    with anyio.move_on_after(timeout):
        async for message in read:
            root = message.message.root
            if isinstance(root, JSONRPCNotification) and root.method == CHANNEL_METHOD:
                got.setdefault(root.params.get("meta", {}).get("kind"), root.params)
                if kinds <= set(got):
                    break
    return got


async def mcp_handshake(read, write) -> dict:
    await mcp_send(
        write,
        JSONRPCRequest(
            jsonrpc="2.0",
            id=1,
            method="initialize",
            params={
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        ),
    )
    resp, _ = await mcp_await_response(read, 1)
    await mcp_send(
        write,
        JSONRPCNotification(
            jsonrpc="2.0", method="notifications/initialized", params={}
        ),
    )
    return resp.result.get("capabilities", {}) if resp else {}


async def mcp_call(
    read, write, request_id: int, name: str, arguments: dict | None = None
):
    """Call a tool; return (text, [channel events seen while waiting])."""
    await mcp_send(
        write,
        JSONRPCRequest(
            jsonrpc="2.0",
            id=request_id,
            method="tools/call",
            params={"name": name, "arguments": arguments or {}},
        ),
    )
    resp, channels = await mcp_await_response(read, request_id)
    return resp.result["content"][0]["text"], channels


async def mcp_list_tools(read, write, request_id: int) -> list[str]:
    await mcp_send(
        write,
        JSONRPCRequest(jsonrpc="2.0", id=request_id, method="tools/list", params={}),
    )
    resp, _ = await mcp_await_response(read, request_id)
    return sorted(t["name"] for t in resp.result["tools"])


__all__ = [
    "DAEMON",
    "RELAY",
    "FakeGitHub",
    "daemon_env",
    "daemon_process",
    "free_port",
    "mcp_await_channel",
    "mcp_await_channel_with",
    "mcp_await_channel_with_kinds",
    "mcp_await_response",
    "mcp_call",
    "mcp_collect_channels",
    "mcp_handshake",
    "mcp_list_tools",
    "mcp_send",
    "relay_env",
    "relay_params",
    "stdio_client",
    "wait_port",
]
