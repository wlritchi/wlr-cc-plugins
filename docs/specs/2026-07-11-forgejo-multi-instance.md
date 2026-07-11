# Forgejo PR notifications: multiple named instances

Status: approved design (2026-07-11), implementation to follow as an additive
fast-follow to the single-instance v1 poller (shipped in 1.7.0). Builds on the
Forgejo/Gitea PR notifications design; does not block or change the v1 deployment.

## Ruling

v1 serves exactly one Forgejo/Gitea instance per daemon (`FORGEJO_API_URL` +
`FORGEJO_TOKEN`). Agents whose PRs live on a *second* instance can't use PR
notifications at all. Multi-instance support is approved with the shape below —
converged independently by the maintainer and the poller author, endorsed from the
deployment side. Everything is additive: v1 configuration, storage keys, wire
messages, and tool output remain byte-identical for the default instance.

## Config: aliased env pairs

- The existing unaliased pair `FORGEJO_API_URL` / `FORGEJO_TOKEN` **is** the
  **default instance**, forever. A daemon configured with only these behaves
  exactly as v1.
- Each additional instance is one pair: `FORGEJO_<ALIAS>_API_URL` +
  `FORGEJO_<ALIAS>_TOKEN`. The alias is lowercase kebab-case in refs and tool
  output; it is uppercased (`-` → `_`) for the env var names.
- Why pairs over a JSON blob: each token stays a discrete secret reference (no
  credentials embedded in a composite env value), and configuring a new instance
  never touches an existing one — deployment diffs are one line per instance.
- The daemon builds one `ForgejoClient` per configured pair at startup
  (the client already takes `base_url`/`token` constructor args). URL
  normalization is unchanged from v1 (`_api_root`): bare instance base or
  `/api/v1` root both accepted, trailing slashes stripped.

## Ref syntax: alias prefix

- Bare `owner/repo#N` → default instance (unchanged).
- `<alias>:owner/repo#N` → the named instance (e.g. `external:owner/repo#12`).
- Full-host refs are deliberately rejected: URL normalization pain
  (scheme/port/trailing slash), and hostnames would leak into transcripts and
  tool output — the alias keeps infrastructure names out of the addressing layer.
- Unknown alias → clear error listing the configured aliases (names only, never
  URLs).
- **Canonicalization**: if an alias happens to be configured for the same
  instance as the default, or a ref explicitly names the default instance's
  alias, it must resolve to the **same unprefixed storage key** as the bare
  ref — otherwise one PR grows two trackers. Simplest rule: the default
  instance has no alias; an alias that resolves to the default's URL is a
  configuration error reported at startup.

## Storage

- `storage_key` gains the alias dimension for non-default instances only:
  `forgejo:<alias>:owner/repo#n`. Default stays `forgejo:owner/repo#n` —
  existing on-disk trackers are byte-identical and need no migration (same
  back-compat pattern as github-unprefixed in v1).
- Ack/notification id parsing already tolerates the extra colon: ids parse by
  `rpartition(':')` and only rely on the trailing components being colon-free
  (verified during v1 review).
- `base_url` stays persisted per tracker as audit and as a mismatch guard: if an
  alias is ever repointed at a different URL, the daemon detects the persisted
  `base_url` disagreeing with the alias's configured URL and skips the tracker
  with a warning rather than silently polling the wrong instance.

## Wire: optional `instance` field + echo-back

- The three FORGEJO verbs gain an optional `instance` field (absent = default).
  The relay parses the alias prefix off the ref and sends
  `{instance, owner, repo, number}`; the daemon resolves `instance → client` and
  derives the storage key. First-class field, not alias-embedded-in-ref on the
  wire: keeps parsing in one place (the relay) and the daemon's contract typed.
- The daemon **echoes** `instance` in its replies.
- Version-skew edge (belt; daemon-first deploy is the primary guard): an old
  daemon ignores the unknown `instance` field, subscribes on its default
  instance, and replies without an echo. The relay must treat
  *sent-non-default-alias + no echo* as **failure**, not success:
  - If the relay has a this-session record that it just created that
    subscription, it unsubscribes the stray default-instance sub it caused.
  - Otherwise — **warn, don't destroy**: report "this daemon predates
    multi-instance; a default-instance subscription to owner/repo#N may have
    been created — check `list_forgejo_pr_subscriptions`". Blind cleanup is
    forbidden because the same `owner/repo#N` can exist on both forges
    (mirrored repos) with a legitimate pre-existing default-instance
    subscription.

## Tool output

- Non-default subscriptions are tagged with the alias: subscribe replies read
  `Subscribed to owner/repo#N (Forgejo: <alias>) …`; list lines read
  `<alias>:owner/repo#N`. The default instance renders exactly as v1 — zero
  churn for existing users.

## Auth errors

- Already per-tracker in v1, so per-instance isolation is structural: one
  instance's auth failure never affects another's trackers.
- The stop-polling/auto-unsubscribe message names the failing instance's alias
  and its exact token env var (e.g. `FORGEJO_EXTERNAL_TOKEN`).

## Sequencing / non-goals

- Explicitly **not** part of the v1 production rollout: the v1 env shape is
  forward-compatible (the default pair never changes meaning), so adding an
  instance later is two new env vars plus an upgraded daemon — no re-shape, no
  migration, no second roll of anything already deployed.
- Per-instance poll cadence/rate budgets, webhook-push event sources, and
  GitHub-side multi-host (GHES) are out of scope.
