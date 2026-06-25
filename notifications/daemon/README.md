# notifications daemon

A persistent, single-instance WebSocket server that owns the notification
schedule for every Claude Code session on this machine. The per-session stdio
MCP relay (`../mcp/notifications-server.py`) connects to it, registers its
session id, forwards schedule requests, receives due notifications, and acks
them. All persistence and dispatch lives here; the relay is stateless.

The relay **never spawns** the daemon — you run it (manually or via systemd).
If it isn't running, the relay keeps retrying to connect and the schedule/list
tools report it as unavailable.

## Configuration (environment)

| Variable                 | Default                   | Purpose                                  |
|--------------------------|---------------------------|------------------------------------------|
| `NOTIFICATIONS_WS_HOST`  | `127.0.0.1`               | WebSocket bind/connect host              |
| `NOTIFICATIONS_WS_PORT`  | `8137`                    | WebSocket port                           |
| `NOTIFICATIONS_DATA_DIR` | `~/.claude/notifications` | Where scheduled callbacks are persisted  |

The relay reads the same `NOTIFICATIONS_WS_HOST`/`PORT`, so if you change the
port, set it for both (e.g. in your shell profile, so Claude Code's MCP servers
inherit it).

## Run manually

```bash
uv run -qs notifications-daemon.py
# or, with overrides:
NOTIFICATIONS_WS_PORT=8137 uv run -qs notifications-daemon.py
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
