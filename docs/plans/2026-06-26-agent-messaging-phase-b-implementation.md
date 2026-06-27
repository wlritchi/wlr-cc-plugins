# Agent Messaging — Phase B Implementation Plan (Message Core & Addressing)

Implements **Phase B** of
[`docs/specs/2026-06-26-agent-messaging-design.md`](../specs/2026-06-26-agent-messaging-design.md):
the message primitive and its addressing schemes (channels + DMs), the intent /
severity / mentions model, and **wake-gating** — the first thing that *consumes*
the per-agent threshold Phase A built. Builds directly on the Phase A directory
([`…-phase-a-implementation.md`](2026-06-26-agent-messaging-phase-a-implementation.md))
and reuses the PR tracker's delivery/ack substrate.

## Scope

- A unified **message topic** (the `PRTracker` analog) with membership,
  append-only events, per-subscriber acked sets, retention, and a topic string.
- **Channels** (`join`/`post`/`leave`) and **DMs** (degenerate channels), both
  over that one primitive.
- The **intent enum**, **severity** (`@here`), and **mentions** (`@someone`) on
  every message.
- **Wake-gating** split across daemon and shim (see the mechanism below).
- Generalized **`catch_up`** as the held-message drain.

## Phase-B design decisions (settled in the spec + this session)

1. **Registration is a prerequisite.** `join`/`post`/`dm` require the calling
   session to be a registered agent (Phase A). That guarantees every participant
   has a name (for mentions / DM addressing) and a global threshold default. Not
   registered → error "register_agent first".
2. **No explicit channel create.** Join-or-post creates; membership = the
   subscriber set; posting auto-joins. Channels are communal (no session
   ownership, no reclaim). Names are kebab-case.
3. **Inactivity-based GC.** A topic is reaped once it has been *both* memberless
   and silent for `NOTIFICATIONS_CHANNEL_TTL_SECONDS` (default 86400), reusing the
   PR reaper loop with a `last_activity` marker.
4. **History on join.** New member's acked-set seeded to all current ids (no
   replay, no wake); the join reply *additionally* renders the last N messages
   (default 20, `NOTIFICATIONS_CHANNEL_HISTORY` knob) as scrollback.
5. **DMs are auto-named channels.** Topic key derived from the sorted participant
   name set, so the same participants always reuse one thread; same retention.
6. **Eager ship, shim-side gating** (the mechanism §). The daemon never withholds;
   the shim decides surface-vs-hold. **Invariant: the shim acks a message only on
   actual surface/delivery, never on receipt.**

## The wake-gating mechanism (the one genuinely new piece)

The daemon/shim boundary is made to mirror the attention model's sender/receiver
split.

**Daemon = sender-side loudness.** In its per-subscriber delivery loop the daemon,
for each message M and recipient R, computes and stamps onto the `NOTIFY` meta:

- `level` ∈ `ambient | direct | urgent`:
  - `urgent` if `M.severity == "high"` (the `@here`);
  - else `direct` if R is *addressed* — R is a DM recipient, or R ∈ `M.mentions`;
  - else `ambient`.
  *(Intent is orthogonal: it carries reply-control/display semantics, not level.
  Reply-to-author addressing arrives with threads in Phase D.)*
- `threshold` ∈ `all | direct | urgent`: R's effective bar for this topic =
  per-topic override (`sub-<sid>.json`) ?? the agent record's `default_threshold`.
- plus `context` (the topic key), `from`, `intent`, `severity`, `mentions`, body.

Then it **ships eagerly** — every unacked message, exactly as the PR path ships
today. The daemon makes *no* surface decision and never needs to know turn state.

**Shim = receiver-side attention.** On receiving a message `NOTIFY` the shim:

- Reduces `level`/`threshold` to the ordinal `ambient<direct<urgent` and computes
  `surface = level >= threshold`.
- **Surface:** hand to the existing debounce coalescer (→ one channel event) and
  **flush any held messages** for this session in the same batch; **ack** each.
- **Hold:** append to an in-memory per-context pending buffer; **do not ack**.
- A later surfacing message (or `catch_up`) flushes the held buffer; debounce
  coalesces. So a lone `fyi` waits; a subsequent `@here` flushes everything.

Because held messages stay **unacked**, a shim crash loses nothing — the daemon
re-ships unacked messages on reconnect and the shim re-holds them. Restart replay
is unchanged.

**Pull mode falls out for free.** A non-channel session can't push, so it never
surfaces proactively — every message is effectively held and drained by
`catch_up`. Gating and pull mode are the same buffer.

**Future (not this phase):** once hooks let the shim tell *active* from *parked*,
an active agent's shim can surface held messages immediately (no wake cost) — a
shim-local change, no daemon involvement. The split is what makes that possible.

**Scope boundary:** gating applies to **message** notifies only. PR and scheduled
notifies keep today's always-surface behavior (the shim passes them straight to
debounce); they simply aren't stamped with a `level`.

## Protocol additions (`lib/wsproto.py`)

Relay → daemon:

| Constant | Wire `type` | Payload |
|---|---|---|
| `JOIN_CHANNEL` | `join_channel` | `{req_id, session_id, channel, threshold?, topic?}` |
| `LEAVE_CHANNEL` | `leave_channel` | `{req_id, session_id, channel}` |
| `POST` | `post` | `{req_id, session_id, channel, body, intent?, severity?, mentions?}` |
| `DM` | `dm` | `{req_id, session_id, to: [name…], body, intent?, severity?}` |
| `SET_THRESHOLD` | `set_threshold` | `{req_id, session_id, context, threshold}` |
| `SET_CHANNEL_TOPIC` | `set_channel_topic` | `{req_id, session_id, channel, topic}` |
| `LIST_CHANNELS` | `list_channels` | `{req_id, session_id}` |
| `LIST_SUBSCRIPTIONS` | `list_subscriptions` | `{req_id, session_id}` |

Daemon → relay:

| Constant | Wire `type` | Payload |
|---|---|---|
| `CHANNEL_JOINED` | `channel_joined` | `{req_id, channel, members, topic, history: [msg…]}` |
| `POSTED` | `posted` | `{req_id, id, context, members}` |
| `CHANNEL_LIST` | `channel_list` | `{req_id, channels: [{name, topic, members, last_activity}]}` |
| `SUBSCRIPTION_LIST` | `subscription_list` | `{req_id, subscriptions: [{context, kind, threshold}]}` |
| *(reuse)* `AGENT_OK` | for `leave`/`set_threshold`/`set_channel_topic` acks |
| *(reuse)* `ERROR` | not-registered, unknown name, invalid threshold, bad channel name |

`NOTIFY` (existing daemon→relay) gains optional message fields: `kind:"message"`,
`context`, `level`, `threshold`, `from`, `intent`, `severity`, `mentions`.

## Message schema, identity, storage

- **Identity / ordering:** the daemon assigns a per-topic monotonic `seq`;
  message id = `msg:<topic_key>:<seq>` (messages are authored once, never
  re-derived, so no content-hash identity is needed; seq gives natural ordering
  and a trivial last-N history tail). Per-subscriber acked sets track these ids,
  exactly as PR events do.
- **Topic keys:** channel → `chan:<name>`; DM → `dm:<sorted,participant,names>`
  (stable, so a participant set reuses one thread).
- **Storage** mirrors `pr/<key>/` under `$NOTIFICATIONS_DATA_DIR/msg/<safe_key>/`:
  - `state.json` — `kind` (`channel`|`dm`), `members` (session ids), `topic`,
    `next_seq`, `last_activity`, retention markers.
  - `events.jsonl` — append-only messages (compacted past `MAX_CACHED_EVENTS`,
    reusing the PR compaction that bumps `missed`).
  - `sub-<sid>.json` — `{acked, missed, threshold?}` (the per-topic threshold
    override lives here).

## New lib modules

**`lib/message_topic.py`** — pure topic logic + persistence (the `PRTracker`
analog; no daemon/WS dependency, clock injected):

```python
@dataclass
class Message:
    id: str; seq: int; sender: str; body: str
    intent: str = "fyi"; severity: str = "normal"
    mentions: tuple[str, ...] = (); created_at: float = 0.0
    def to_dict(self)->dict: ...   # ; from_dict

class MessageTopic:
    key: str; kind: str; topic: str
    members: set[str]; acked: dict[str,set[str]]; missed: dict[str,int]
    thresholds: dict[str,str]            # session_id -> per-topic override
    messages: list[Message]; next_seq: int; last_activity: float
    def join(self, sid, *, now, threshold=None) -> None      # seed acked = all ids
    def leave(self, sid) -> None
    def post(self, sender_sid, *, now, body, intent, severity, mentions) -> Message
    def history_tail(self, n: int) -> list[Message]
    def reapable(self, *, now, ttl) -> bool                  # memberless AND silent
    # load()/persist helpers via lib/storage.py
```

**`lib/messaging.py`** — pure helpers shared by daemon and shim:

```python
INTENTS = frozenset({"fyi", "question", "request", "reply"})  # reaction = Phase C
_LEVELS = {"ambient": 0, "direct": 1, "urgent": 2}            # == threshold scale

def compute_level(*, severity: str, addressed: bool) -> str   # urgent/direct/ambient
def effective_threshold(override: str | None, default: str) -> str
def should_surface(level: str, threshold: str) -> bool        # level >= threshold
def dm_key(names: list[str]) -> str                           # sorted, prefixed
def channel_key(name: str) -> str
def validate_channel_name(name: str) -> None                  # kebab-case, reuse agent rule
```

Reuse `lib/storage.py` (`atomic_write`, `safe_name`, `load_json_dir`) and the
agent registry for name→session resolution / threshold defaults.

## Daemon wiring (`daemon/notifications-daemon.py`)

- `TOPICS: dict[str, MessageTopic]` loaded on startup (mirrors `TRACKERS`);
  `_channel_ttl_seconds()` (default 86400) + `_channel_history_n()` (default 20).
- Handlers (mirror the PR/agent ones; `session_id = conn.session_id or msg[...]`;
  require `REGISTRY.get_by_session(session_id)` or `ERROR`):
  - `join_channel` → get-or-create topic, `topic.join(...)`, persist, reply
    `CHANNEL_JOINED` with members + topic + `history_tail(N)`; `_wake(session_id)`.
  - `leave_channel` → `topic.leave`, persist, `AGENT_OK`.
  - `post` → resolve channel (auto-create + auto-join sender), `topic.post(...)`,
    persist + append, bump `last_activity`, wake every member, reply `POSTED`.
  - `dm` → resolve `to` names→sids via `REGISTRY` (unknown → `ERROR`), get-or-create
    `dm:` topic with members = participants, post, wake, reply `POSTED`.
  - `set_threshold` → store override on the topic's `sub-<sid>.json`; `AGENT_OK`.
  - `set_channel_topic` / `list_channels` / `list_subscriptions` → straightforward.
- **Delivery loop:** where it builds a message `NOTIFY`, stamp `kind:"message"`,
  `context`, `from`, `intent`, `severity`, `mentions`, and the per-recipient
  `level` (`compute_level(severity, addressed = sid in mentions or DM-recipient)`)
  and `threshold` (`effective_threshold(topic.thresholds.get(sid),
  record.default_threshold)`). Ship eagerly — **no daemon-side gating**.
- **Reaper:** extend the existing reaper to also delete topics where
  `topic.reapable(now, _channel_ttl_seconds())`.

## Relay wiring (`mcp/notifications-server.py`)

- **Gating layer** just upstream of the existing debounce: a per-session
  `held: dict[context, list[NOTIFY]]`. On a message `NOTIFY`
  (`meta.kind == "message"`): if `should_surface(level, threshold)` → push to
  debounce + drain `held` into the same flush + **ack all**; else append to
  `held` (no ack). Non-message notifies bypass the gate (unchanged).
- **`catch_up`** (generalize existing): drain `held` (all contexts) into the
  return value and **ack** them; in pull mode this is the only delivery path, so
  behavior there is unchanged/strengthened.
- **Tools** (FastMCP, agent-facing docstrings, `effective_session_id`, reuse
  `_daemon_request`; all return strings):
  - `join_channel(channel, threshold=None, topic=None)` — reply shows members +
    topic + rendered history tail.
  - `leave_channel(channel)`
  - `post(channel, body, intent="fyi", severity="normal", mentions=None)` —
    `mentions` a list of agent names; reply confirms + member count (so a typo
    surfaces as "you're the only member").
  - `dm(to, body, intent="request", severity="normal")` — `to` a list of names.
  - `set_threshold(context, threshold)` / `set_channel_topic(channel, topic)`
  - `list_channels()` / `list_subscriptions()` — the latter shows each context's
    threshold.
  - Docstrings explain intent (`fyi`/`reaction` are terminal — no reply expected),
    `severity:"high"` = `@here`, and `mentions` = `@someone`.

## Build slices (subagent → review → commit each)

**Slice 1 — message core + leveling (pure lib + storage).** `lib/message_topic.py`,
`lib/messaging.py`, and `tests/test_unit_messaging.py` (join-seeds-acked,
post-assigns-seq, history_tail, reapable, compute_level matrix, effective_threshold,
should_surface, dm_key stability, channel-name validation, persistence round-trip).
No daemon/WS. *Accept:* `run.py -k messaging` green, ruff clean.

**Slice 2 — daemon + shim vertical.** wsproto constants; daemon topic handlers +
membership/lifecycle + delivery level/threshold stamp + eager ship + reaper
extension; relay gating layer + ack-on-surface + `catch_up` generalization + the
tools. One happy-path e2e: A and B join `#x`; A posts `@here` → B surfaces; A posts
a plain `fyi` while B's threshold is `direct` → B does **not** surface it but
`catch_up` drains it. *Accept:* that e2e + tool-list smoke green, ruff clean.

**Slice 3 — e2e coverage.** DMs (incl. multi-recipient thread reuse), mentions →
direct surface, the flush-on-clear batching, history-on-join tail, topic display,
`set_threshold` changing surface behavior, and GC (a memberless+silent topic
reaped under a low TTL). *Accept:* full suite green, ruff clean.

*(Slice 2 is the meaty one; if its diff balloons, split into 2a daemon / 2b shim
against the protocol table above.)*

## Testing notes

- Reuse `agent_session` (push relay) and the two-session pattern; add a
  pull-mode session helper if needed to exercise `catch_up`-only delivery.
- Assert surface-vs-hold by watching channel events (`mcp_await_channel_with`) vs.
  confirming a message only appears via `catch_up`.
- Inject `now` in unit tests; bound every e2e wait (reuse `_list_until`-style
  polling). Set low `NOTIFICATIONS_CHANNEL_TTL_SECONDS` for the GC test.

## Versioning

Do not bump manually — `version-bump.yml` handles both manifests on push to main.

## Out of scope (later phases)

Reactions + read receipts + `message_status` (Phase C); threads / `reply_to` /
reply-to-author addressing, feeds, role-spawn, hook-based active/parked detection
(Phase D). The gating split is built so the last of these is a shim-only change.
