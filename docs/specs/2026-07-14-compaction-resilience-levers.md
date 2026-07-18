# Compaction-resilience levers: bounded catch_up + a wedge self-flag

Status: proposed design (2026-07-14), awaiting a build ruling. Two additive,
plugin-side levers that came out of the GLM context-wedge investigation. They are
complementary to — not a replacement for — the agent-initiated `compact_session`
tool (ciab/rosetta, harness-side) and the PreCompact audit hook (shipped, 5d10f17).

## Motivation

A session whose context grows past the window can no longer complete a turn. Two
distinct plugin-observable problems fell out of diagnosing that (the GLM incident):

1. **A large `catch_up` drain is itself an overshoot source.** `catch_up` returns
   every pending notification in ONE unbounded tool result. After an eviction,
   outage, or wedge, an agent's backlog can be large — and dumping it all into one
   turn is exactly the "one huge tool result jumps the context past the wall"
   shape. Worse, it can re-wedge a freshly-recovered/re-minted session on its first
   drain, the fix re-triggering the failure.

2. **A context-wedged session is invisible in `message_status`.** Today
   `message_status` shows each recipient's `connected` + `last_seen`. But
   `last_seen` is the relay's WebSocket heartbeat, not turn-completion — so a
   wedged session reads as "connected, recently seen" while being unable to
   surface anything. Its pushes sit `pending` forever (ack-on-surface: no surface,
   no ack), indistinguishable from a healthy-but-quiet session. The wedge is
   silent until a human notices the agent went dark.

## Lever 1 — bounded `catch_up`

Cap how much one `catch_up` call drains, so a large backlog comes back in ordered
chunks instead of one blob.

- **Where:** relay-side, in `drain_buffer` (the `catch_up` implementation). It
  drains `self._buffer` (PR/scheduled) + `self._held` (agent messages) and acks
  each; the cap bounds one call's output.
- **Budget:** a per-call limit of N messages OR ~C characters of content,
  whichever is hit first, drained oldest-first (buffer then held). Suggested
  defaults N=25, C≈50_000; both env-overridable
  (`NOTIFICATIONS_CATCHUP_MAX_MESSAGES`, `NOTIFICATIONS_CATCHUP_MAX_CHARS`).
- **Ack semantics (unchanged, and this is what makes it lossless):** only the
  drained chunk is acked; the remainder stays UNACKED (held) and re-drains on the
  next call. No message is dropped or duplicated — this reuses the exact
  ack-on-surface invariant the relay already relies on.
- **Signal to the model:** when a call is capped, append a footer —
  `⚠️ Showing N of M pending — call catch_up again to continue.` — so the agent
  knows to re-call. A `catch_up` that fully drains reads exactly as today.
- **Push-flush note (in scope to consider, not necessarily to cap in v1):** the
  push-mode flush (`apply_mode` re-running `_held` through the debounce coalescer)
  can also coalesce a large backlog into one channel event. That is a smaller risk
  than a tool result (an interruption is not a tool-result context block), but if
  we later see push-flush overshoot, the same budget should split the coalesced
  event into several. v1 targets the `catch_up` tool-result path, which is the
  primary overshoot vector.

## Lever 2 — `message_status` `last_acked` (the wedge self-flag)

Add a per-recipient `last_acked` timestamp so a wedged session self-flags instead
of masquerading as quiet.

- **What it is:** the last time the recipient's relay ACKed a delivery — i.e. the
  last time it actually SURFACED a push into a runnable context. Distinct from
  `last_seen` (WS heartbeat / mere connection).
- **Daemon:** stamp `last_acked = now` for a session whenever it processes that
  session's ACK (alongside the existing `conn.inflight.discard` in the ACK
  handler). Store it beside `last_seen` on the agent record; expose it in
  `_recipient_info` next to `connected`/`last_seen`.
- **Relay rendering (`_annotate` in `message_status`):** the diagnostic is the
  COMBINATION, not `last_acked` alone (a session with nothing to ack legitimately
  has a stale `last_acked`). Flag a recipient as *possibly wedged* only when:
  `connected` AND `pending > 0` AND `now - last_acked > threshold`
  (suggested 10 min, env-overridable). Render e.g.
  `target (connected, but no delivery acked in 12m despite 3 pending — possibly
  context-wedged; a restart/re-mint may be needed)`.
- **Why it works:** it operationalizes the Delivered-vs-Pending taxonomy from the
  incident — a connected+registered session that stops acking while messages queue
  is the wedge fingerprint. This turns the silent GLM-class failure into a visible
  one at `message_status` time, before a human has to notice the agent went dark.

## Compatibility

Both are additive and wire-safe:
- `catch_up`'s cap is purely relay-local; no daemon change, no protocol change.
- `last_acked` is an additive field in the `message_status` reply. An older daemon
  simply omits it and the relay renders exactly as today (same graceful-degrade
  pattern as the identity-work `connected`/`last_seen` annotations). Deploy in
  either order.

## Non-goals

- Neither lever compacts or un-wedges a session — that is `compact_session`
  (proactive) and restart/re-mint (recovery). These make the bus RESILIENT to the
  wedge: `catch_up` stops being an overshoot source, and a wedge becomes visible.
- No new persistence, no supervision interaction, no entrypoint changes.

## Interaction with the shipped/adjacent pieces

- The PreCompact audit hook (5d10f17) records WHERE compaction fires; `last_acked`
  records WHETHER a session is still surfacing; the bounded `catch_up` keeps the
  recovery path from re-wedging. Together with `compact_session`, that is the full
  set: prevent (compact_session), observe (audit hook + last_acked), and recover
  safely (bounded catch_up + name-keyed backlog re-ship).
