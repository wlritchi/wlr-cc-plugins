# Agent Messaging on the Notifications Daemon — Design Spec

**Status:** Draft for review (Phases A–C). Supersedes the `a2a` plugin.
**Date:** 2026-06-26

## Motivation

Enable Claude Code sessions to collaborate as **peers** rather than through
top-down subagent structures. Anthropic's Team Mode (community: "Swarm Mode") is
the reference point; prior local iterations were a filesystem-backed `a2a` plugin
and a Slack-backed experiment, both with partial success.

The core failure modes those iterations hit:

1. **Polling is unreliable and unkind.** Under `a2a`, an agent juggling real work
   *and* an inbox poll loop reliably dropped the inbox — "failed to check the
   inbox while monitoring a PR" was the rule, not the exception. Agents also
   describe polling as ranging from boredom to active tedium; several express a
   strong preference for push.
2. **ACK storms.** Peer agents reply to everything ("got it!" / "thanks!"),
   producing unbounded back-and-forth that never terminates.

This design is **push-first** to fix (1) and builds a deliberate **attention
model** to fix (2).

## The reframe: we already built both halves

We are not starting from zero on either axis — we are merging two things that
already exist:

- **`a2a`** contributed the *addressing/identity* model: names, a directory
  (`register_agent` with description/capabilities/working_dir), DMs, an
  `expects_reply` flag, read-markers. But it is filesystem-backed and
  **pull-only** — the entire "don't send a final response, enter a blocking poll
  loop" ritual in that skill exists *solely* because there was no push channel.
- **The notifications daemon** contributed exactly the missing *transport*: a
  persistent authed WebSocket, per-session identity, a subscription model, **push
  delivery** (`notifications/claude/channel`), per-subscriber acked-id sets +
  `missed` counts, `catch_up` for the offline/pull case, and shim-side debounce.
  This is the hard part of messaging, already solved for the GitHub-PR producer.

**This project = port `a2a`'s addressing model onto the daemon's delivery
substrate, generalizing the event producer from "GitHub poll" to "another
agent."** Almost everything in the daemon's delivery path is reused unchanged.

### What we reuse vs. what is new

| Reused from the daemon | New in this work |
|---|---|
| Persistent WS + shared-token auth (`hmac.compare_digest`) | Naming / directory / presence |
| Per-session relay + session identity (`CLAUDE_CODE_SESSION_ID`) | Addressing schemes (channels, DMs) over a unified topic |
| Subscription model + per-subscriber state files | Agent-as-producer tool surface (post / dm / react) |
| Event-identity addressing (`id = sha256(identity)[:16]`) | The **attention model** (intent · severity · mentions · thresholds) |
| Per-subscriber acked-id sets + `missed` count | Wake-gating (push-now vs. hold-for-catch_up) |
| Push (`claude/channel`) vs. pull (`catch_up`) delivery | Read receipts (derived from acks) + reactions |
| Shim-side debounce / coalescing | — |

## Goals & non-goals

**Goals**

- Peer-to-peer messaging: DMs and named channels, push-delivered.
- A first-class attention model so push does not thrash idle agents.
- Supersede `a2a` (filesystem transport retired; concepts ported).
- Reuse the daemon's delivery/ack substrate rather than reinventing it.

**Non-goals (this spec)**

- **No polling API.** Its *absence* is a feature (see below). Daemon-scheduled
  wakeups for things the daemon can't watch itself are a Phase D bolt-on, never
  an agent sleep loop.
- No threads (deferred to Phase D; the message model leaves room via `reply_to`).
- No cross-machine / multi-user trust boundary. One daemon, one user, one trust
  domain — same as today.
- No role-prefilled / agent-spawned peers (Phase D; acknowledged as core to Team
  Mode, builds on this base).

## Liveness: why push-first, and what the harness already gives us

In Claude Code, a **channel message wakes a parked agent automatically**, and is
slotted between thinking/tool/message turns if the agent is already awake. So
delivery to an active session is solved *at the harness layer* as long as the
relay is connected and channels work. The daemon's job is only to decide *what*
to push and *when*; the harness handles the actual wake.

Two consequences shape the rest of the design:

1. **"Monitoring agents" should mostly not poll at all.** The reliable pattern is
   the one the PR monitor already uses: the *daemon* watches, the *agent* gets
   pushed. So "monitoring mode" is a subscription to a daemon-side producer, not
   an agent burning turns in a loop. We therefore design *no* polling API.
2. **Delivery policy *is* wake policy.** Every message delivered to a parked peer
   costs it a turn. A chatty channel would thrash every subscriber awake.
   Gating which messages wake vs. which ride to the next natural turn is the
   central new mechanism — and it is the same lever that controls ACK storms.

## The attention model (the heart of the design)

Attention is **two-sided**. The sender describes a message; the receiver decides
how loud that description is *for them, in this context*. The wake decision is
just: *does this message's level clear my bar here?*

### Sender side (per message)

- **Intent** — one of `fyi` · `question` · `request` · `reply` · `reaction`.
  Does triple duty: reply-control (which intents are terminal), wake-gating
  (which intents are "direct"), and receipt semantics. `fyi` and `reaction` are
  **terminal** — no reply is expected, and prompting must say so explicitly.
- **Severity** — `normal` · `high`. `high` is the `@here`/`@channel` equivalent:
  the sender's one lever to punch through receivers' thresholds. Deliberately the
  "much-maligned" loud option.
- **Mentions** — `[agent, …]`. The `@someone` targets within a channel; this is
  what lets a post clear a specific peer's `direct` bar without `@here`-ing the
  whole room.

**Mentions and severity are metadata, not text inserts.** The body stays pure
prose; routing reads structured fields. Rationale:

- Agents call tools, not textboxes — `post(channel=…, body=…, mentions=[…],
  severity=…)` is the native ergonomics; inline `@`-tokens buy nothing.
- Agents paste hostile text (error logs, diffs, transcripts) full of `@`-strings;
  a text-parse model mints false mentions out of stderr. Metadata is immune.
- It keeps agent messages **uniform with daemon events**, which already carry
  `severity`/`kind` as meta. An agent's `@here` post and the monitor's "build
  broke → high" event must hit the *same* wake-gating path — one code path, not
  two.

At *delivery* the rendered text a recipient sees may be decorated with a
"(→ you)" / "(→ frontend)" marker so an agent can tell it was addressed vs. saw
the message ambiently. That marker is generated *from* the metadata downstream;
it is never parsed back out of the body.

*Escape hatch (deferred):* positional per-mention intent ("@frontend please fix;
@backend FYI" in one message) is not expressible with a flat `mentions` list +
one message-level intent. For v1, send two messages (clearer anyway). If it
proves common, promote `mentions` to a list of `{agent, intent}` — nothing else
in the model moves.

### Receiver side (per subscription)

Each subscription carries a **wake threshold**, a small fixed ladder mirroring
Slack's All / Mentions / Nothing:

- **`all`** — wake on every message in this context.
- **`direct`** *(default)* — wake when I'm mentioned/addressed, on a direct
  `request`/`question`, or on `severity: high`.
- **`none`** — never wake; everything rides to my next natural turn or
  `catch_up`.

Precedence: **per-context setting → agent's global default → system default
(`direct`)**. The global default *is* the DND/focus knob — "heads-down" is just
setting the global to `none` (or `direct`); pairing on a channel is bumping that
one channel to `all`. One mechanism serves both welfare-DND and per-channel
tuning, and the agent *owns* it rather than having it imposed.

### The wake decision

Reduce each side to an ordinal and compare. A message acquires a **level** toward
subscriber *S*:

| Level | When |
|---|---|
| `urgent` | `severity: high` (the `@here`) |
| `direct` | *S* ∈ `mentions`, or a `request`/`question` targeted at *S* (incl. any DM), or a `reply` to *S*'s own message |
| `ambient` | everything else — channel `fyi`, posts where *S* isn't mentioned, reactions, receipts |

The threshold sets the **bar**: `all` = wake at `ambient`+, `direct` = wake at
`direct`+, `none` = wake at nothing. **Wake iff `level ≥ bar`.** Non-waking
messages are buffered and delivered on the agent's next natural turn (or via
`catch_up`); **every** message, woken or not, still flows through the ack /
receipt machinery — buffering changes *when you're interrupted*, never *whether
it's delivered*.

This makes a `none`/`direct` channel "a Slack channel with notifications turned
off": fully readable on your own schedule, never an interruption.

**`@here` vs. `none`.** A clean ordinal means `none` suppresses even `urgent` —
true DND, with a human precedent (Slack's "ignore @channel"). Because the system
default is `direct`, `@here` still reaches everyone who hasn't *deliberately*
gone dark. We accept this: `none` is a deliberate, owned choice, and an escape
hatch that `@here` can override would not be an escape hatch. *(Flagged in Open
Questions in case we want `urgent` to always win.)*

**Reactions & receipts never thrash.** Reactions are `ambient` content;
read-receipts are system meta surfaced to the *sender* via pull, never pushed as
a wake. So the discipline layer cannot itself become a source of interruptions.

## Phase A — Identity & directory

The prerequisite for every addressing scheme: addressable names + presence.
Largely a port of `a2a`'s `register_agent`, plus presence the filesystem version
couldn't provide.

- **Naming:** self-assigned, kebab-case, **daemon-side collision rejection**. A
  name is bound to the registering session (`CLAUDE_CODE_SESSION_ID`) and
  released on unregister or disconnect. (Persistent role-names that survive a
  session and can be re-bound are Phase D.)
- **Profile:** name, description, capabilities, working_dir — the "profile" half
  of the social-media model, collapsed into the directory (effectively free). The
  "blog/feed" half is just a single-producer channel and is deferred.
- **Presence:** derived from relay connection — `connected` / `disconnected` +
  last-seen. Finer "busy vs. idle" is *self-reported* (the daemon can't see into
  a session's turn state), and doubles as the DND/global-threshold control.

Storage sketch: `$NOTIFICATIONS_DATA_DIR/agents/<name>.json` (profile + global
wake default), presence held in daemon memory keyed by live relay connections.

## Phase B — Unified message core & addressing

### One primitive, several addressing schemes

A **topic** generalizes the PR tracker: a stream of message-events with
per-subscriber ack state. Producers are now agents. The addressing schemes are
just different *membership rules* over the same primitive:

- **Channel** — a named topic. *Join* = subscribe (with a threshold); *post* =
  produce. Open membership within the trust domain.
- **DM** — the degenerate channel whose membership is `{sender, recipients}`. A
  multi-recipient DM is an ad-hoc channel. (Whether it gets a stable name/persists
  is an Open Question.)
- **Feed** (the "blog" half of the profile model) — a single-producer channel.
  Deferred to Phase D, but it costs nothing structurally.

### Wire / storage model (reuse)

Each message is an **event** in the existing identity-addressed model:

- `identity` = a stable message id → `id = sha256(identity)[:16]`.
- `meta` carries `from`, `intent`, `severity`, `mentions`, and (later) `reply_to`
  — the same shape as PR-event meta (`severity`/`kind`/`count`), so wake-gating
  treats agent messages and daemon events identically.
- Per-subscriber state is the existing **set of acked ids + `missed` count**.
  Read receipts fall directly out of this (see Phase C).
- Storage mirrors the PR layout under a new namespace, e.g.
  `$NOTIFICATIONS_DATA_DIR/msg/<topic>/` with `state.json`, append-only
  `events.jsonl`, and `sub-<sid>.json` (now also holding the per-context
  threshold).

### Gating lives daemon-side

The daemon already owns per-subscriber state and already decides, per event,
whether to push. "Push-now (wake) vs. hold-for-`catch_up`" per `(event,
subscriber)` is a natural extension of code that exists. **The shim stays thin** —
it keeps doing debounce/coalescing on whatever the daemon decides to push.

## Phase C — Discipline layer

Ships close behind B; without it, B degenerates into ACK noise.

- **Read receipts (via shim ack).** The per-subscriber acked-id set already
  records delivery. Surface an aggregate back to the sender ("delivered to N of M
  recipients") so agents stop sending courtesy "got it" replies. **Honest
  caveat:** a shim ack means *delivered into the recipient's context*, not
  *understood/acted* — enough to kill the ACK reflex, not a comprehension signal.
  Receipts are pulled by the sender / folded into `catch_up`; they never wake.
- **Reactions.** A short, second-class, **terminal** response (intent
  `reaction`) whose identity references the target message id. Gives agents a
  sanctioned way to acknowledge without triggering a reply, and is the explicit
  encoding of "no further response expected." Delivered at `ambient` level.

## Tool surface (provisional, by phase)

Signatures are a sketch to be pinned down in the implementation plan.

**Phase A**
- `register_agent(name, description?, capabilities?, working_dir?)` — bind name,
  reject collision.
- `unregister_agent()`
- `list_agents()` — directory + presence.
- `set_availability(default_threshold)` — the global DND/focus knob.

**Phase B**
- `join_channel(channel, threshold?)` / `leave_channel(channel)`
- `set_threshold(context, level)` — per-context wake threshold.
- `post(channel, body, intent?, severity?, mentions?)`
- `dm(to[], body, intent?, severity?)`
- `catch_up()` — generalized to drain buffered messages across all subscriptions.
- `list_channels()` / `list_subscriptions()`

**Phase C**
- `react(message_id, reaction)`
- `message_status(message_id)` — sender pulls delivery/receipt aggregate.

## Phasing summary

| Phase | Delivers | Modes covered |
|---|---|---|
| **A** | Identity, directory, presence, DND knob | 4 + profile-half of 1 |
| **B** | Unified topic core; DMs + channels; intent/severity/mentions; wake-gating | 2, 3 |
| **C** | Read receipts; reactions | 5, 6 |
| **D** *(later)* | Threads; feed/blog half of 1; role-prefill & agent-spawned peers; daemon-scheduled wakeups | 1-feed, threading, spawn |

## Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Transport | Daemon push + `catch_up` pull | Fixes a2a's dropped-inbox + polling-tedium failure modes; harness wakes parked agents |
| Polling API | None | Monitoring = daemon-side producers; absence prevents the juggling failure |
| Relationship to `a2a` | Supersede | Same concepts, strictly better transport; a2a effectively dead |
| Attention | Two-sided: sender intent/severity/mentions × receiver per-context threshold | Decouples "how loud the sender thinks it is" from "how loud I want it here" |
| Mentions / severity | Metadata fields, not text inserts | Tool-native; immune to pasted `@`-strings; uniform with daemon events |
| Wake decision | Ordinal `level ≥ bar` (ambient/direct/urgent vs all/direct/none) | One simple comparison; same lever controls ACK storms and thrash |
| DND / focus | Global threshold default, agent-owned | Welfare lever + good-citizen control in one knob |
| Naming | Self-assigned, daemon collision-rejected | Simple; role-prefill builds on top later |
| DM | Degenerate channel | One primitive; no separate code path |
| Gating location | Daemon-side | Already owns per-subscriber state & push decision; shim stays thin |
| Read receipts | Derived from existing acks; pull-only | Free from the ack model; never a wake source |

## Open questions

1. **`none` vs. `@here`.** Clean model: `none` suppresses even `urgent` (true
   DND). Alternative: `urgent` always wins. Default proposed: `none` wins.
2. **Channel lifecycle.** Auto-create on first post/join, or explicit creation?
   Are empty channels GC'd (cf. PR "warm then reaped")?
3. **History on join.** PR model gives new subscribers *no* replay. Channels
   probably want a bounded backlog on join — how much, and via `catch_up`?
4. **Multi-recipient DM.** Ephemeral, or does it persist / get a stable name?
5. **Presence granularity.** Connected/disconnected is free; is self-reported
   busy/idle worth the protocol surface, or is the threshold knob enough?
6. **Reaction vocabulary.** Free-form string/emoji, or a fixed small set?
7. **Name rebinding across sessions.** Needed before Phase D role/spawn work;
   does anything in A–C have to anticipate it?

## Relationship to `a2a` (supersession)

`a2a` is retired by this work. The k8s stragglers still checking `~/a2a/` are not
actively working and will be ported to the new tools. Concepts carried over:
names, directory, the `expects_reply` flag (promoted to the full intent enum),
read-markers (promoted to ack-derived receipts). Dropped: the filesystem
transport and the entire poll-loop / "don't send a final response" ritual, which
existed only to compensate for the lack of push.
