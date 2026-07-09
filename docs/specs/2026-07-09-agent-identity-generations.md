# Agent identity: name-keyed messaging + generations

Status: approved design (2026-07-09), implementation in slices.
Supersedes the "targeted membership migration" fix for the mid-reclaim message-loss
bug; builds on the Phase A–C agent-messaging design (2026-06-26).

## Ruling

**Name = service, session = process.** An agent name is the durable, public identity;
a session is one process currently animating it. Session IDs leave the public
messaging interface entirely — they remain only as audit/forensics attributes on
registry records and in logs. Names are never shared between concurrently-live
agents; the registry's one-name-one-holder rule is unchanged.

## Problem being solved

DM threads are keyed by participant *names*, but topic membership (and acked/missed/
threshold state) is keyed by *session id*, resolved at send time. A message sent while
a name maps to a dying session queues against the dead sid; a successor reclaiming the
name was never a member, so the message strands forever ("mid-reclaim DM loss").
Dead sids also linger as topic members ("ghost participants"). Any fix that migrates
sid-keyed state on reclaim treats the symptom; keying membership by name removes the
class.

## Changes

### (a) Topic membership keyed by name; session resolved at delivery time

- `MessageTopic` member keys become agent **names**. The lib already treats member
  keys as opaque strings; only its docs/comments change. All messaging ops already
  require a registered agent, so every joiner has a name.
- Daemon: `join`/`leave`/ack/threshold/`delivery_status` use `record.name`.
  `_dispatch_loop` resolves `conn.session_id → registry record → name` and delivers
  topics where that *name* is a member. A successor registering the name is, by
  construction, a member of everything its predecessor was in, with the same acked
  set — unsurfaced messages re-ship on its first REGISTER. Nothing to migrate at
  reclaim time; ghost participants cease to exist.
- On-disk: `sub-<sid>.json` becomes `sub-<name>.json`. One-time lazy migration on
  topic load: a sub file whose key matches a current registry record's `session_id`
  is rewritten under that record's name; a sub file whose key matches no record and
  no member name is a ghost — dropped (its acked state is meaningless without a
  holder). No eager rewrite of anything else; a daemon rollback sees name-keyed sub
  files as unknown-sid members that age out at topic reap, losing nothing that the
  old code could have delivered anyway.

### (b) Generation counter per name

- `AgentRecord.generation: int` (default 1). `register()` increments it when the
  name transfers to a **different session** (grace-expiry reclaim, keyed reclaim, or
  takeover of an offline holder); a same-session idempotent re-register keeps it.
  Persisted with the record; returned in `AGENT_OK` and shown in `list_agents`.
- Fencing: sends and acks already resolve the caller's session to its registry
  record; a displaced session resolves to nothing and is refused ("not registered").
  The generation makes the refusal *explainable* and gives the relay a durable fact
  to compare: the AGENT_OK reply carries `(name, generation)`, the relay persists it
  (pod persistent storage — box-image coordination with claude-in-a-box), and a
  relay that reconnects and finds the name at a higher generation held by another
  session knows it has been superseded rather than merely disconnected.
- The registry keeps `session_id` on records for audit only; no tool output requires
  it (list_agents may retain it as a forensic attribute).

### (c) message_status recipient annotation

`delivery_status` output (now name-keyed) is annotated per recipient with
`connected` (name's current session has a live connection) and `last_seen` (registry
record), so "delivered 3h ago to an agent idle since then" is visible to the sender.
"Delivered" continues to mean acked-on-surface — it asserts the message reached the
session context, never that a turn ran.

### (d) Succession events

On a generation increment for name X (gen N → N+1):

1. **Fleet-visible notice:** the daemon posts a system-authored message
   ("⟳ succession: X gen N→N+1") to the well-known channel `#system` (auto-created,
   any agent may join; ambient severity — visible in catch_up/history, wakes nobody).
   Knowing a handoff happened explains behavior the way knowing a pod rolled
   explains a cold cache.
2. **Heir notice to the predecessor:** if the superseded session still has a live
   connection, the daemon sends it a direct system notification ("an heir has
   appeared: X is now gen N+1, held by another session") at `direct` level, enabling
   an explicit predecessor→heir handoff (the predecessor can DM the name it used to
   hold — delivery-time resolution routes it to the heir).

## Wire/compat

Additive only: new fields (`generation` in AGENT_OK/AGENT_LIST, annotations in
STATUS_RESULT), no new verbs, no changed shapes. Old relays ignore the new fields.
Daemon-first deploy as usual. The relay-side generation bookkeeping is a separate,
optional relay change (a relay that ignores generations behaves exactly as today).

## Slices

1. **(a) name-keyed membership** — fixes the loss bug; daemon + lib docs + sub-file
   migration + tests (unit: membership/ack by name, migration, ghost-drop;
   e2e: DM sent mid-roll reaches the successor).
2. **(b) generations** — registry field + increment rule + AGENT_OK/list surfaces +
   tests. Relay bookkeeping follows separately with claude-in-a-box.
3. **(c) status annotation** — small, rides with (b).
4. **(d) succession events** — `#system` post + heir notice + tests.

## Non-goals

- Reworking reclaim-key issuance/rotation (still the deferred holistic redesign;
  the settle window remains a stopgap).
- Any change to channel-mode detection or ack-on-surface semantics.
