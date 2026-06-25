# notifications daemon

A persistent, single-instance WebSocket server that owns notification state for
every Claude Code session on this machine. The per-session stdio MCP relay
(`../mcp/notifications-server.py`) connects to it, registers its session id, and
exchanges notifications. All persistence and dispatch lives here; the relay is
stateless.

The relay **never spawns** the daemon — you run it (manually or via systemd).
If it isn't running, the relay keeps retrying to connect and the tools report it
as unavailable.

It provides two capabilities:

- **Scheduled one-shot callbacks** — proof-of-concept (`schedule_test_notification`).
- **GitHub PR monitoring** — `subscribe_github_pr(org/repo#number)` polls the PR
  for checks, reviews, comments and mergeability and pushes rich notifications
  (merge conflicts, short comments inline, inline-comment line ranges, links).
  Each poll is a single GraphQL query (few API calls); rate-limit headers are
  honoured (it throttles as the budget runs low) and failures are classified
  (auth / not-found / rate-limited / transient) so recovery is tailored to each.
  Polling backs off (5 min, doubling after every 2 idle polls, up to 8 h) but is
  capped to ~1 h during business hours (8am ET–8pm PT, Mon–Fri). Each subscriber
  has an acked high-water mark; new subscribers join without replay; polling
  suspends when no subscribed session is connected; a merged PR auto-unsubscribes
  everyone. Requires `GITHUB_TOKEN` in the daemon's environment.

## Configuration (environment)

| Variable                      | Default                   | Purpose                                       |
|-------------------------------|---------------------------|-----------------------------------------------|
| `NOTIFICATIONS_WS_HOST`       | `127.0.0.1`               | WebSocket bind/connect host                   |
| `NOTIFICATIONS_WS_PORT`       | `8137`                    | WebSocket port                                |
| `NOTIFICATIONS_DATA_DIR`      | `~/.claude/notifications` | Where callbacks and PR state are persisted    |
| `GITHUB_TOKEN`                | —                         | Token for PR polling (`gh auth token`)        |
| `GITHUB_API_URL`              | `https://api.github.com`  | API base (set for GitHub Enterprise / tests)  |
| `GITHUB_GRAPHQL_URL`          | `{GITHUB_API_URL}/graphql`| GraphQL endpoint (override for Enterprise)    |
| `NOTIFICATIONS_PR_POLL_SECONDS` | —                       | Force a fixed PR poll cadence (override/testing) |

The relay reads the same `NOTIFICATIONS_WS_HOST`/`PORT`, so if you change the
port, set it for both (e.g. in your shell profile, so Claude Code's MCP servers
inherit it). `GITHUB_TOKEN` only needs to be set for the daemon.

## Run manually

```bash
GITHUB_TOKEN=$(gh auth token) uv run -qs notifications-daemon.py
# or, with overrides:
NOTIFICATIONS_WS_PORT=8137 GITHUB_TOKEN=$(gh auth token) uv run -qs notifications-daemon.py
```

It logs `notifications daemon listening on ws://127.0.0.1:8137` to stderr. Only
one instance can bind the port; a second one exits with an error.

## Run via systemd --user

1. Copy/symlink the unit and **edit the two paths in its `ExecStart`** (the path
   to `uv`, from `command -v uv`, and the path to this script):

   ```bash
   mkdir -p ~/.config/systemd/user
   cp notifications-daemon.service ~/.config/systemd/user/
   ${EDITOR:-nano} ~/.config/systemd/user/notifications-daemon.service
   ```

2. Enable and start it:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now notifications-daemon
   systemctl --user status notifications-daemon
   journalctl --user -u notifications-daemon -f
   ```

   (If you want it to keep running while you're logged out:
   `loginctl enable-linger $USER`.)
