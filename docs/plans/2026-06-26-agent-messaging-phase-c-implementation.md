# Agent Messaging — Phase C Implementation Plan (Discipline: Receipts & Reactions)

Implements **Phase C** of
[`docs/specs/2026-06-26-agent-messaging-design.md`](../specs/2026-06-26-agent-messaging-design.md):
the discipline layer that keeps channels from drifting into ACK noise. Two small
features on top of the Phase B message core — both reuse machinery that already
exists.

## Scope

- **Read receipts** — surface, to any topic member, who has actually received a
  message (derived from the per-subscriber acked sets we already maintain), via a
  `message_status(message_id)` pull. Never pushed, never wakes.
- **Reactions** — a short, **terminal** acknowledgment (`react(message_id, …)`)
  that rides the existing message pipeline at `ambient` level, so it's seen but
  never wakes. The sanctioned way to say "got it" without triggering a reply.

## Why these reuse what we have

- A receipt is just "is this message id in member X's acked set?" The shim's
  **ack-on-surface** invariant (Phase B) makes that acked set *meaningful*:
  `acked` = actually delivered into the recipient's context (surfaced or drained
  via `catch_up`), not merely shipped. A held sub-threshold message reads as
  **pending** until the recipient drains it — a genuine "hasn't seen it yet"
  signal, not a guess.
- A reaction is just a message with `intent="reaction"` and a `target` pointing
  at the message id it reacts to. It flows through the Phase B delivery path
  unchanged, except its level is forced to `ambient`.

**Honest caveat (carried in the tool text):** a receipt means *reached the
agent's context*, not *read / understood / acted*. Enough to retire courtesy
"got it" replies; not a comprehension signal.

## Phase-C design decisions

1. **Reactions are forced `ambient`** regardless of addressing or DM-ness —
   `compute_level` gains an `intent` argument and returns `ambient` for
   `reaction`. A reaction never wakes anyone (including the target's author); the
   author sees it on their next turn / `catch_up` / in `message_status`. This is
   what "terminal, second-class" means mechanically.
2. **Reactions are real messages** in the topic log (`intent="reaction"`,
   `target=<message id>`), so they persist, appear in scrollback/`catch_up`, and
   are found by scanning the log — no separate store. (No reaction *to* a
   reaction is expected; the intent itself signals "don't respond".)
3. **Vocabulary: free-form but short** (1–`REACTION_MAX` chars, no newlines) —
   a single emoji or a terse token. Open question #2 (fixed set vs free-form)
   resolved toward free-form for v1; a fixed set can be layered later if it gets
   noisy. Flagged, not blocking.
4. **Membership required.** `react` / `message_status` require the caller to be a
   member of the target message's topic (reuse the `_resolve_sender` + membership
   checks); reacting does **not** auto-join.
5. **`message_status` is open to any member**, not just the author — a channel is
   communal. The author is excluded from its own message's recipient tally (it
   pre-acks its own post).

## Lib changes (`lib/message_topic.py`, `lib/messaging.py`)

- `Message` gains `target: str = ""` (the reacted-to message id; empty for normal
  messages), threaded through `to_dict`/`from_dict`. *(Distinct from a future
  Phase D `reply_to` for threads — don't conflate.)*
- `MessageTopic.delivery_status(message_id) -> tuple[list[str], list[str]]` —
  `(delivered_sids, pending_sids)` partitioning current members by whether
  `message_id ∈ acked[sid]`. Pure.
- `MessageTopic.reactions_for(message_id) -> list[tuple[str, str]]` —
  `[(reactor_name, reaction_body)]` from log messages where
  `intent == "reaction"` and `target == message_id`. Pure.
- `messaging.compute_level(*, severity, addressed, intent="fyi") -> str` — add the
  `intent` param; `reaction` ⇒ `ambient` (checked first, before severity). Default
  keeps existing callers working.
- `messaging.validate_reaction(reaction: str) -> None` (1–`REACTION_MAX` chars, no
  newline) raising `InvalidReaction(MessagingError)`.

## Protocol additions (`lib/wsproto.py`)

Relay → daemon:

| Constant | Wire `type` | Payload |
|---|---|---|
| `REACT` | `react` | `{req_id, session_id, target, reaction}` |
| `MESSAGE_STATUS` | `message_status` | `{req_id, session_id, target}` |

Daemon → relay:

| Constant | Wire `type` | Payload |
|---|---|---|
| `MESSAGE_STATUS_RESULT` | `message_status_result` | `{req_id, delivered: [name…], pending: [name…], reactions: [{by, reaction}]}` |
| *(reuse)* `AGENT_OK` | react ack |
| *(reuse)* `ERROR` | not a member / unknown message / invalid reaction |

## Daemon wiring (`daemon/notifications-daemon.py`)

- A small `_topic_for_message(message_id)` helper: parse the topic key out of
  `msg:<topic_key>:<seq>` (reuse the `_handle_ack` `msg:` parsing), return the
  `MessageTopic` (or None).
- `_handle_react` → resolve sender (registered) + topic from `target`; verify the
  target message exists and the caller is a member; `validate_reaction`; `post` a
  message with `intent="reaction"`, `target=<id>`, `body=<reaction>`; persist;
  wake members (they won't surface it — it's ambient — but a concurrent clearing
  message would flush it); `AGENT_OK`.
- `_handle_message_status` → resolve sender + topic from `target`; verify
  membership; `delivered, pending = topic.delivery_status(target)` mapped sids→
  names and with the author's name removed from both; `reactions =
  topic.reactions_for(target)`; reply `MESSAGE_STATUS_RESULT`.
- **Delivery loop:** pass `message.intent` into `compute_level(...)` so reactions
  ship at `ambient`. Render a reaction specially, e.g.
  `[#room] agent-b reacted "👍" to agent-a's "<snippet>"` (look the target up in
  the same topic for the snippet; fall back gracefully if it has been compacted
  out).

## Relay tools (`mcp/notifications-server.py`)

- `react(message_id: str, reaction: str) -> str` — sends `REACT`; confirms.
  Docstring: a reaction is terminal — it acknowledges without asking for a reply,
  and never wakes the recipient; use it instead of a "got it" message.
- `message_status(message_id: str) -> str` — sends `MESSAGE_STATUS`; renders
  "Delivered to N of M: …; pending: …; reactions: 👍 agent-b" plus the
  reached-context-not-read caveat.

## Build slices (subagent → review → commit each)

**Slice 1 — lib extensions + unit tests.** `Message.target`, `delivery_status`,
`reactions_for`, `compute_level(intent=…)`, `validate_reaction`, and
`tests/test_unit_messaging.py` additions (reaction⇒ambient across the severity/
addressed matrix; delivery_status partition incl. a held/unacked member as
pending; reactions_for filters by target+intent; target round-trips; reaction
validation bounds). *Accept:* `run.py -k messaging` green, ruff clean.

**Slice 2 — vertical + e2e.** wsproto constants; `_handle_react` /
`_handle_message_status` / `_topic_for_message` + the delivery-loop intent pass
and reaction rendering; the two relay tools. e2e (`test_e2e_messaging.py`
additions): a reaction is held (ambient) for the author — no channel event — yet
appears in their `catch_up` and in `message_status`; `message_status` shows a
recipient as pending while a post is held below their threshold, then delivered
after they `catch_up`; reaction validation rejected loudly. *Accept:* full suite
green, ruff clean.

## Testing notes

- Reuse `agent_session`, `mcp_call`, `mcp_await_channel_with`, the `count()`
  request-id pattern; bound every wait. To prove a reaction never wakes: assert
  `mcp_await_channel_with(..., timeout=small) is None`, then that `catch_up` /
  `message_status` surfaces it.
- Inject `now` in unit tests; no wall-clock assertions.

## Versioning

Do not bump manually — `version-bump.yml` handles both manifests on push to main.

## Out of scope (Phase D)

Threads / `reply_to` / reply-to-author addressing, feeds, role-spawn, hook-based
active/parked detection, a fixed reaction vocabulary. The `target` field is for
reactions only; threading gets its own field later.

## Addendum — global message handles (`#N`)

`react`/`message_status` take a message id, but slice 2 left no agent-facing path
that *surfaces* one, so the tools aren't usable until ids are discoverable.
Decision: expose a **global per-message ordinal** rendered `#N` — chosen over
git-style hash prefixes because it's stable, never ambiguous, and uniform across
channels and DMs (the message-volume it leaks is meaningless in a single-user
daemon, and "tracking handles" collapses to incrementing a counter).

Design:

- **Ordinal.** Every posted message (reactions included) gets a daemon-global
  monotonic `ordinal: int` (from 1). `Message` gains the field
  (`to_dict`/`from_dict`); `MessageTopic.post` gains an `ordinal` param — the lib
  stays counter-agnostic, the daemon allocates.
- **Counter.** Persisted at `<data_dir>/msg/.next_ordinal`; on startup
  `next = max(file, 1 + max ordinal across loaded messages)` so it's strictly
  monotonic across topic reaps and restarts; advanced + persisted per post.
- **Resolution.** `_message_by_ordinal(n)` scans `TOPICS` for the message with
  that ordinal (globally unique ⇒ ≤1 match; `react`/`message_status` are
  low-frequency, so a scan beats maintaining an index). `_resolve_target(s)`
  accepts `#N`, `N`, or a full `msg:` id and returns `(topic, message)`; the full
  id is always an escape hatch. Authorization stays per-topic (caller must be a
  member of the resolved message's topic).
- **Surfacing.** Every rendered line leads with the handle, e.g.
  `#147 [#room] agent-a: ship it (→ you)`; a reaction references its target's
  handle: `#150 [#room] agent-b reacted "👍" to #147 (agent-a's "<snippet>")`.
  `POSTED` gains `ordinal`, so `post`/`dm` replies read "… (#147)"; the join
  history tail and `catch_up` render handles too.
- **Tools.** `react`/`message_status` accept `#N` / `N` / full id (daemon resolves
  via `_resolve_target`).

Build: one vertical (lib field + daemon counter/resolver/render + relay reply &
history rendering + the two tools) with unit tests (ordinal assignment,
by-ordinal + handle-or-id resolution, render includes the handle) and e2e (react
and message_status driven by a `#N` the agent reads from a post reply / channel
line — the end-to-end usability this addendum exists to deliver).
