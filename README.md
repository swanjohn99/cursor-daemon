# Cursor Daemon

Zero-token Telegram bot relay for Cursor Agent. Routes messages directly to
`cursor-relay.sh`, bypassing the LLM entirely.

## Architecture

```
Telegram → daemon.py (dict-check) → cursor-relay.sh → Cursor Agent
                ↓ (no match)
          ❌ Unknown command → error back to Telegram
```

No LLM tokens consumed. No Hermes agent involved.

## Files

| Path | Purpose |
|---|---|
| `~/.cursor-daemon/daemon.py` | Main daemon |
| `~/.cursor-daemon/state.json` | Bot configs, project assignments, logs |
| `~/.config/systemd/user/cursor-daemon.service` | Systemd unit |
| `~/work/<project>/.agent-lock/` | Mutual exclusion (shared with Hermes) |

## Port

**9000** — binds to `0.0.0.0` for Tailscale access.

Web UI: `http://localhost:9000` or `http://<tailscale-ip>:9000`

## Setup

```bash
systemctl --user daemon-reload
systemctl --user enable cursor-daemon
systemctl --user start cursor-daemon
```

## Web UI

- **Add bot** — name + Telegram token + optional project assignment
- **Assign project** — dropdown per bot, saves instantly
- **Remove bot** — ✕ button
- **Logs** — last 50 entries, auto-refresh every 10s
- No auth — protected by Tailscale network

## Commands

Same as Hermes passthrough relay:

| Command | Does |
|---|---|
| `1`–`13` | Select project by number |
| `projects` / `back` / `go back` | Show project list |
| `cursor <prompt>` | Send prompt to Cursor Agent (`agent -p`) |
| `plan on` / `plan off` | Toggle plan mode |
| `mode agent` / `mode plan` / `mode ask` / `mode shell` | Switch Cursor mode |
| `model <name>` | Set Cursor model |
| `status` | Current project, mode, model |
| `lock` | Show lock status |
| `force` | Override stale lock |
| `help` | Show all commands |

Anything else → `❌ Unknown command`

## Management

```bash
systemctl --user status cursor-daemon   # Check status
systemctl --user restart cursor-daemon  # Restart
systemctl --user stop cursor-daemon     # Stop
journalctl --user -u cursor-daemon -f   # Live logs
```

## Dependencies

- Python 3.11+ (system)
- `aiohttp` (already installed)
- `python-telegram-bot` ≥22 (already installed)
- `cursor-relay.sh` at `~/.hermes/profiles/cursor-2/scripts/`