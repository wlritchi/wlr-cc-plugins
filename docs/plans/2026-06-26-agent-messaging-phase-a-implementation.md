# Agent Messaging — Phase A Implementation Plan (Identity & Directory)

Implements **Phase A** of
[`docs/specs/2026-06-26-agent-messaging-design.md`](../specs/2026-06-26-agent-messaging-design.md):
the addressable-identity layer every later phase sits on. No messaging yet — this
delivers a daemon-backed **agent directory** with presence and the per-agent
focus/threshold knob.

## Scope

Four relay tools and their daemon-side machinery:

- `register_agent(name, …)` — bind a self-chosen name to this session.
- `unregister_agent()` — release it.
- `list_agents()` — the directory, with live presence.
- `set_availability(default_threshold)` — the global wake-threshold (DND) knob.

## Phase-A design decisions (committed here; flag at review if wrong)

1. **Two registration layers, kept separate.** The existing connection-level
   `REGISTER` (`{session_id}`, sent automatically on relay connect) is unchanged
   and unrelated. Joining the *agent directory* is a distinct, **explicit**
   `register_agent` call. A session can be connected (receiving PR notifications)
   without being a named agent.
2. **One identity per session.** A session owns at most one agent name.
   Re-registering updates the existing record (including a rename, subject to the
   collision check below). This keeps the session↔identity mapping 1:1 for
   Phase A; multi-identity / role-spawn is Phase D.
3. **Presence is derived, not stored.** `connected` = `session_id in CONNECTIONS`
   at query time (the daemon's existing liveness signal). Only `last_seen` is
   persisted (stamped on disconnect), to drive name-reclaim grace and display.
4. **Lazy name reclaim (no background reaper in A).** A name owned by a *live*
   session, or by a session disconnected less than `NOTIFICATIONS_AGENT_TTL_SECONDS`
   ago (default 900s, `<=0` ⇒ reclaim immediately once disconnected), is taken →
   reject. Otherwise a colliding `register_agent` reclaims it. A resumed session
   keeps the same `session_id`, so it hits the idempotent-update path, never the
   collision path. Stale-but-unclaimed records simply show as `offline` in
   `list_agents`; a background reaper is deferred.
5. **The threshold knob is built now but inert until Phase B.** `set_availability`
   persists `default_threshold ∈ {all, direct, urgent}` (default `direct`).
   Nothing consumes it yet — Phase B's wake-gating reads it. Storing it now keeps
   it part of the identity record where it belongs.
6. **No SKILL/command yet.** A directory with no messaging isn't independently
   useful to teach. Tool *docstrings* carry usage guidance for Phase A; the skill
   lands with Phase B messaging.

## Protocol additions (`lib/wsproto.py`)

Relay → daemon:

| Constant | Wire `type` | Payload |
|---|---|---|
| `REGISTER_AGENT` | `register_agent` | `{req_id, session_id, name, description?, capabilities?, working_dir?, default_threshold?}` |
| `UNREGISTER_AGENT` | `unregister_agent` | `{req_id, session_id}` |
| `LIST_AGENTS` | `list_agents` | `{req_id, session_id}` |
| `SET_AVAILABILITY` | `set_availability` | `{req_id, session_id, default_threshold}` |

Daemon → relay:

| Constant | Wire `type` | Payload |
|---|---|---|
| `AGENT_OK` | `agent_ok` | `{req_id, agent}` — the resulting record (or `{name}` for unregister) |
| `AGENT_LIST` | `agent_list` | `{req_id, agents: [record + "connected": bool]}` |
| *(existing)* `ERROR` | `error` | `{req_id, error}` — collisions, invalid name/threshold, not-registered |

Replies correlate via the existing `req_id` echo in `_send(...)`.

## Storage schema

Mirror the per-PR layout. One file per agent under
`$NOTIFICATIONS_DATA_DIR/agents/<safe_name>.json` (reuse the `_safe(...)`
slugging used for PR keys):

```json
{
  "name": "frontend",
  "session_id": "abc123…",
  "description": "",
  "capabilities": "",
  "working_dir": "",
  "default_threshold": "direct",
  "registered_at": 1750000000.0,
  "last_seen": 1750000000.0
}
```

`connected` is never written — it is computed at `list_agents` time.

## New module: `lib/agent_registry.py` (pure logic + persistence)

Testable with no daemon/WS. Clock is injected (`now: float` params) to match the
repo's existing test style — no `time.time()` buried in logic paths under test.

```python
@dataclass
class AgentRecord:
    name: str
    session_id: str
    description: str = ""
    capabilities: str = ""
    working_dir: str = ""
    default_threshold: str = "direct"
    registered_at: float = 0.0
    last_seen: float = 0.0
    # to_dict() / from_dict()

class AgentRegistry:
    def __init__(self, data_dir: Path) -> None: ...        # loads agents/*.json
    def register(self, session_id: str, name: str, *, now: float,
                 is_session_live: Callable[[str], bool], ttl: float,
                 description: str = "", capabilities: str = "",
                 working_dir: str = "",
                 default_threshold: str | None = None) -> AgentRecord: ...
    def unregister(self, session_id: str) -> AgentRecord | None: ...
    def set_availability(self, session_id: str, default_threshold: str) -> AgentRecord: ...
    def touch(self, session_id: str, now: float) -> None: ...   # stamp last_seen
    def get_by_session(self, session_id: str) -> AgentRecord | None: ...
    def list(self) -> list[AgentRecord]: ...
```

Rules:

- **Name validation:** kebab-case, 2–64 chars, `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$`
  → `InvalidName` otherwise.
- **Threshold validation:** in `{all, direct, urgent}` → `InvalidThreshold`.
- **Collision** (`register`, target `name` already exists, owned by a *different*
  session): reject with `NameTaken` if `is_session_live(owner)` **or**
  `now - owner.last_seen < ttl`; else reclaim (overwrite).
- **Rename / one-per-session:** if this `session_id` already owns a different
  name, release that record (delete its file) before binding the new name.
- **Idempotent update:** same session re-registering its own name updates profile
  fields in place.
- Mutations persist immediately (atomic write / unlink); `unregister` and reclaim
  delete the freed file.

Exceptions (`NameTaken`, `InvalidName`, `InvalidThreshold`, `NotRegistered`)
subclass a common `AgentRegistryError(ValueError)`; the daemon maps them to
`ERROR` replies with the exception message.

## New module: `lib/storage.py`

Factor the atomic-write helper so the new registry doesn't depend on
`pr_monitor`:

```python
def atomic_write(path: Path, text: str) -> None: ...   # mkdir -p, tmp + os.replace
def load_json_dir(directory: Path) -> list[dict]: ...  # read every *.json, skip bad
```

(Optional, low-risk follow-up — not required for A: repoint `pr_monitor._atomic_write`
at this. Leave the tested PR code alone for now.)

## Daemon wiring (`daemon/notifications-daemon.py`)

- **Startup:** `REGISTRY = agent_registry.AgentRegistry(data_dir)`;
  `AGENT_TTL = _agent_ttl_seconds()` from `NOTIFICATIONS_AGENT_TTL_SECONDS`.
- **Dispatch:** add four branches to `_handle(...)` mirroring the subscribe
  handlers; `is_session_live = lambda sid: sid in CONNECTIONS`.
  - `register_agent` → `REGISTRY.register(..., is_session_live=is_session_live,
    ttl=AGENT_TTL)`; reply `AGENT_OK{agent}` or `ERROR`.
  - `unregister_agent` → `REGISTRY.unregister(conn.session_id)`; reply `AGENT_OK`.
  - `set_availability` → `REGISTRY.set_availability(...)`; reply `AGENT_OK`/`ERROR`.
  - `list_agents` → decorate each record with `connected` and reply `AGENT_LIST`.
- **Disconnect (`finally`):** if `conn.session_id`, `REGISTRY.touch(session_id,
  time.time())` so the reclaim-grace clock starts and `last_seen` is fresh.
  (Presence flips automatically when `CONNECTIONS` drops the entry.)

## Relay tools (`mcp/notifications-server.py`)

FastMCP `@mcp.tool()` async functions returning strings, using
`session_state.effective_session_id()` and `DAEMON.request(...)` exactly like the
PR tools. Good docstrings (they are the only Phase-A usage guidance):

- `register_agent(name, description="", capabilities="", working_dir="", default_threshold="direct")`
  → "Registered as 'frontend' (wake threshold: direct). Other agents can find you
  via list_agents." / on `ERROR`: surface the reason (e.g. name taken).
- `unregister_agent()` → confirmation or "You weren't registered."
- `list_agents()` → formatted directory: each agent with presence
  (`connected` / `offline, last seen …`), description, capabilities, cwd, and
  wake threshold.
- `set_availability(default_threshold)` → validate/echo; docstring explains the
  ladder: `all` (wake on every message), `direct` (default — wake on mentions /
  direct requests / `@here`), `urgent` (wake only on `@here`).

## Build slices (one focused subagent each; review + commit between)

**Slice 1 — registry core + storage.** `lib/storage.py`, `lib/agent_registry.py`,
and `tests/test_unit_agents.py` (register, idempotent update, rename, collision
while live, reclaim after grace, unregister, set_availability, name/threshold
validation, persistence round-trip via a fresh `AgentRegistry` on the same dir).
No daemon/WS. *Accept:* `run.py -k agents` green, ruff clean.

**Slice 2 — protocol + daemon + relay vertical.** `lib/wsproto.py` constants;
daemon handlers + disconnect `touch`; the four relay tools. Add **one** happy-path
e2e (`register_agent` on relay A, `list_agents` on relay B sees it `connected`)
plus the harness helpers it needs. *Accept:* that e2e + a tool-list smoke green,
ruff clean.

**Slice 3 — e2e coverage.** Round out `tests/test_e2e_agents.py`: two-agent
directory, collision rejected across live sessions, `set_availability` reflected
in `list_agents`, disconnect → `offline`, `unregister` → gone. Harness polish as
needed. *Accept:* full suite green under the default interpreter, ruff clean.

## Testing notes

- Reuse the multi-relay pattern from the PR fan-out e2e (`test_e2e_pr.py`) to run
  two sessions against one daemon.
- Drive tools via `mcp_call(read, write, req_id, "list_agents", {})`; parse the
  returned text (or assert on substrings).
- Inject `now` in unit tests; never assert on wall-clock.
- Run: `uv run -qs notifications/tests/run.py` (filter `-k agents` during dev).

## Versioning

**Do not bump manually.** `version-bump.yml` auto-bumps both `plugin.json` and
`marketplace.json` on push to main by analyzing the diff. Feature commits stay
version-neutral; CI adds the `chore: bump versions` commit.

## Out of scope (later phases)

Messaging/channels/DMs, the intent enum, reactions, receipts, wake-gating that
*consumes* the threshold (all Phase B/C); background offline reaper; the agent
SKILL/command; multi-identity & role-spawn (Phase D).
