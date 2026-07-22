# Local agent spawn (daemon-launched background sessions)

Status: DRAFT — awaiting build ruling. Launch mechanics incorporate the box
maintainer's authoritative headless-dispatch recipe (2026-07-21).

## Motivation

In managed deployments, provisioning a new agent means editing deployment config and
rolling infrastructure — there is a human/infra layer that can create sessions. Local
(single-workstation) deployments have no equivalent: an agent that wants a peer must ask
the user to start one by hand. This feature brings self-provisioning to local
deployments: the daemon, when explicitly enabled, can launch a new background Claude
Code session seeded with a caller-supplied initial message, and agents reach it through
an MCP tool. The effect approximates configuring a new managed agent, without the
infrastructure layer.

## Scope and non-goals

- **Local deployments only.** The spawn capability is OFF by default and must be
  explicitly enabled by the operator on the machine where the daemon runs. Managed /
  containerized daemon deployments never enable it; provisioning there stays with the
  deployment config layer.
- The daemon spawns sessions **on its own host** (the daemon process is the parent).
  There is no remote-spawn: a relay talking to a remote daemon gets whatever the
  daemon's host policy says (for managed daemons: refusal).
- v1 spawns **background/headless** sessions only. No interactive terminals, no
  resume-into-existing-session, no lifecycle management beyond launch (stopping a
  spawned session is out of scope for v1 — it exits when its work ends, or the operator
  kills it).

## Enablement and gating

Opt-in, two independent conditions, both required:

1. `NOTIFICATIONS_SPAWN_ENABLED=1` in the daemon's environment (default: absent =
   disabled). This is the operator's explicit statement that this daemon may create
   processes on this host.
2. Belt: the daemon refuses to enable spawn if it detects it is running in a managed
   container context (e.g. `KUBERNETES_SERVICE_HOST` present), even if the env var is
   set. Fail-closed misconfiguration guard, not a security boundary.

Additional operator knobs (all env, all with safe defaults):

- `NOTIFICATIONS_SPAWN_ROOTS` — colon-separated list of directory roots under which a
  spawned session's working directory must fall. REQUIRED when spawn is enabled (no
  default = no spawnable cwd = effectively disabled). The daemon resolves the requested
  cwd (realpath) and requires it to be an existing directory under one of the roots.
- `NOTIFICATIONS_SPAWN_MAX_ACTIVE` — cap on concurrently-alive spawned sessions
  (default 4). At the cap, spawn requests fail with a clear error.
- `NOTIFICATIONS_SPAWN_COOLDOWN_SECONDS` — minimum interval between spawns fleet-wide
  (default 30). Backstop against runaway spawn loops (including transitive: a spawned
  agent spawning more agents).

## Wire protocol

New request verb (relay → daemon):

```
SPAWN_AGENT = "spawn_agent"
  {req_id, session_id, initial_message, working_dir, name?}
```

- `initial_message`: the first prompt delivered to the new session. Prompt TEXT only —
  it is never interpolated into a shell command (see Security).
- `working_dir`: absolute path; validated against `NOTIFICATIONS_SPAWN_ROOTS`.
- `name`: optional agent name the new session should register on the bus. Advisory: the
  daemon passes it to the child via its launch instructions; actual registration follows
  the normal register_agent path (name-keyed identity rules apply unchanged — a spawn
  cannot displace a live holder).

Reply (daemon → relay):

```
SPAWN_RESULT = "spawn_result"
  {req_id, pid, session_id?, working_dir, name?}
```

or `ERROR {req_id, error}` with a specific reason (disabled, bad cwd, at cap, cooldown,
launch failure). Old daemons reply `ERROR "unknown message type: spawn_agent"` via the
existing unknown-verb path, so skew fails loudly — no capability handshake needed for
correctness.

## Capability discovery (tool exposure)

The relay registers a `spawn_agent` MCP tool unconditionally (FastMCP tools are static;
dynamic tool lists would complicate the relay for little gain). The tool's docstring
states it only works on daemons with spawn enabled; on an unsupported/disabled daemon
the call returns the daemon's ERROR text verbatim ("spawn is not enabled on this
daemon", or the unknown-verb error on old daemons). This matches how other
deployment-dependent tools already behave and keeps v1 free of a handshake protocol.
If later needed, a `features` field on the AGENT_OK register ack can advertise spawn so
the relay can annotate the tool result proactively.

## Security model

Spawn converts a bus message into process creation on the daemon host — the highest-
privilege verb the daemon has. Constraints, all load-bearing:

- **Fixed argv template.** The daemon builds the child command line from configuration
  and validated fields only. `initial_message` is passed as data (a single argv
  element — never through a shell, `shell=False` always). No caller-controlled flags,
  no caller-controlled binary path. Belt: `shell=False` stops SHELL injection but not
  flag injection — a message starting with `-`/`--` could be parsed by claude's own
  arg parser as an option. Place the message after an explicit `--` end-of-options
  marker (or prefix-wrap it in template text) so a leading-dash message is always data.
- **cwd allowlist** (`NOTIFICATIONS_SPAWN_ROOTS`), realpath-resolved to kill
  `..`/symlink escapes.
- **Rate limit + concurrency cap** (above) bound the blast radius of a compromised or
  confused agent, including transitive spawn chains.
- **Audit + announce.** Every spawn attempt (allowed or refused) is appended to a
  daemon-side audit log (requester session_id + registered name, cwd, name, timestamp,
  outcome), and every successful spawn is announced as a system event so the fleet and
  the operator can see it happen. No silent spawns.
- The existing transport trust model (shared token, trust-on-declare session identity)
  is unchanged and applies here: any token-holder can invoke spawn. The env-gate
  ensures the verb simply does not exist on deployments where that trade-off is
  unacceptable. Per-agent authentication remains a separate, parked workstream.

## Launch mechanics

Based on the box maintainer's authoritative recipe (their headless-dispatch path is the
proven shape for exactly this: a background session seeded with an initial message that
receives follow-up work over the bus).

**Invocation (fresh session):**

```
claude --bg "<initial_message>" \
  --name "<logical-name>" \
  --dangerously-load-development-channels <channel specs>
```

- `--bg` mints a job under `~/.claude/jobs/<short>/` and cold-starts a transient
  supervisor daemon that owns it. The initial message is the positional argument — this
  answers open question 2 below: argv prompt, exactly as the harness expects; no bus
  bootstrap needed.
- `--dangerously-load-development-channels` registers the notifications channel without
  the interactive confirm dialog (a headless worker cannot answer one). Default spec
  `plugin:notifications@wlr-cc-plugins` is auto-selected when `NOTIFICATIONS_WS_URL` is
  present.
- A fresh `--bg` always creates a new session id; `claude respawn <short>` reconstructs
  a stopped job with the SAME session id and injects no prompt. So "new agent with
  initial message" is always `--bg`, never respawn.

**Child environment (set by the daemon on the spawned process):**

- `CLAUDE_BG=1`, `CLAUDE_BG_NAME=<name>` (also defaults the reclaim key),
  `CLAUDE_BG_PROMPT` (positional arg wins; set for belt).
- `NOTIFICATIONS_WS_URL` + `NOTIFICATIONS_TOKEN` inherited from the daemon's own
  config so the child's relay reaches the same bus.
- `NOTIFICATIONS_RECLAIM_KEY`: per-child (defaulted from the child's own name) — NEVER
  a shared host-wide key (the reclaim-key collision class: a sibling sharing the key
  can steal a live agent's name).
- `CLAUDE_CODE_DAEMON_COLD_START=transient` — REQUIRED; without it the daemon
  cold-start takes an interactive path, fatal for headless.
- Model/effort: model is frozen into the job at dispatch (a change needs a fresh
  `--bg`); effort is ambient (re-read on every start).

**Working directory:** cwd at dispatch is the job's identity anchor (`state.cwd`;
resume matching is cwd-scoped). The daemon chdirs to the validated `working_dir` before
dispatch; one workspace ↔ one job lineage.

**Lifetime and supervision — v1 is launch-only, by design.** An idle background job
SETTLES after ~60 minutes: the worker exits, its relay (an MCP child) dies with it,
and the transient harness daemon follows ~5s later. A settled job cannot receive bus
pushes until something respawns it. The managed-deployment answer is an automatic
settle-watch + respawn loop; v1 here deliberately does NOT build one. On a
workstation the operator is present and interacts with spawned agents directly
(`claude agents`, `claude attach <short>`): keeping a session alive past settle is
the OPERATOR's call, made by pinning/respawning the sessions they care about
(`claude respawn <short>`). The daemon spawns; the human supervises. This cuts the
whole watcher subsystem (log tailing, respawn-flag reinjection, restart re-adoption)
from v1. If a respawn loop is ever added, the known hazards are documented in the
reference recipe: liveness comes from `daemon.log` lines (never process argv), the
channels flag must be reinjected into stored `respawnFlags` before every respawn, and
fresh-vs-respawn decisions compare MODEL only (effort is ambient and stored nowhere).

**Shared `~/.claude` — a feature, not a bug.** Spawned agents share the operator's
`~/.claude` (credentials, settings, jobs registry, memory store). The purpose of
spawning here is context COMPARTMENTALIZATION — separate conversations/workspaces for
separate concerns — not state isolation: a shared memory store across the operator's
agents is desired. This also keeps spawned jobs first-class in the operator's own
`claude agents` view. Per-agent `CLAUDE_CONFIG_DIR` isolation was considered and
rejected for v1.

**Cold-start:** first `uv run` of the relay on a cold cache can exceed Claude Code's
30s MCP connect timeout, and a server that misses the window is never retried for the
session's lifetime — the agent comes up with no push and no notification tools. The
daemon MUST pre-warm before dispatch: `uv sync --script` on both the relay
(`mcp/notifications-server.py`) and the session-start hook (`hooks/session-start.py`).

**Permissions mode:** if (and only if) the operator opts spawned agents into
`--dangerously-skip-permissions`, dispatch requires `bypassPermissionsModeAccepted:
true` in `~/.claude.json` — `claude --bg` refuses otherwise. Since the child shares
the operator's config, the daemon treats this as a precondition CHECK with a clear
error; it never edits the operator's config file itself.

**MAX_ACTIVE accounting (no watcher needed):** job dirs are NOT a liveness signal —
`~/.claude/jobs/` accumulates history (dozens of `state: done` dirs persist after
their sessions end, and `state.json` is session-self-reported, so a crashed worker
can leave a stale non-done state). Live sessions come from `claude agents --json`,
the harness's supported scripting surface (it reports each active session's short
id, pid, name, and status; internally it reads the supervisor's roster, but the CLI
is the stable contract). "Active" = (shorts the daemon recorded spawning, in
`spawned.json`) ∩ (ids reported live). Spares and the operator's own sessions never
count against the cap — only recorded spawns can intersect. Evaluated lazily at
spawn time; no background task.

## Open questions

1. ~~argv vs stdin vs bus for the initial message~~ — resolved: positional argv prompt
   (`claude --bg "<msg>"`), the harness's native shape.
2. ~~stop_agent~~ — resolved: NOT a daemon verb in v1. The operator stops/pins
   sessions directly (`claude agents`, job-dir deletion); the daemon only launches.
3. ~~Respawn ownership across daemon restarts~~ — moot in v1: there is no respawn
   loop. `spawned.json` persists only for MAX_ACTIVE accounting.
4. Whether the spawned child should also get `--dangerously-skip-permissions` (the
   reference path supports it) — default NO for workstation spawns; operator can opt
   in via env if their local threat model accepts it.
