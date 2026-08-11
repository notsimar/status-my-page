# status-my-page

> 🌐 Personal service status dashboard with 3-state health indicators, persistent history, mobile-responsive dark theme, and deploy scripts. View-only by default — admin login required to manage.

## ✨ Features

- **3-State Status System**: Click services to cycle 🟢 Operational → 🟡 Degraded → 🔴 Outage
- **Smart Notes**: Auto-show hidden notes only for degraded/outage states, auto-hide on green
- **Status History**: Every toggle and notes update is timestamped — open the 🕙 history panel per service to see the full change timeline
- **Dark Theme UI**: Responsive layout (≤640px & ≤425px breakpoints), mobile-first CSS with proper touch targets
- **Admin Controls**: Session-based auth, drag-and-drop reorder, inline rename, add/delete items, auto-saving notes
- **Config-Driven**: `config.yaml` controls everything — service names, credentials, server settings
- **Auto-Archival**: Pre-reset DB snapshots (JSON into `archives/`) saved on every restart so state survives seeding
- **YAML Backup Rotation**: Last 5 versions of `config.yaml` preserved automatically before each runtime save

## 🚀 Quick start

```bash
# Clone & setup
git clone https://github.com/notsimar/status-my-page.git
cd status-my-page

# Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Generate a password hash (NEVER commit plaintext passwords)
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password'))"

# Copy config.yaml.example → config.yaml and edit your services, then:
STATUS_ADMIN_PASS_HASH=<hash> ./start.sh
open http://localhost:8920  # Default user: admin
```

> **⚠️ Change the default password** via `STATUS_ADMIN_PASS_HASH` environment variable before exposing!

## 📱 Status states at a glance

| State     | Emoji           | Notes behavior                                |
|-----------|-----------------|-----------------------------------------------|
| Operational | 🟢 Green      | Hidden automatically                          |
| Degraded  | 🟡 Yellow      | Visible — admin can add context notes         |
| Outage    | 🔴 Red         | Always visible with pulsing red glow          |

*Click any service as admin to cycle through states ♪*

## 🕙 Status History

Every mutation (status toggle, notes update) is recorded in a `status_history` SQLite table.

- **View**: Click the 🕙 clock icon on any service row → modal shows newest-first timeline
- **API**: `GET /api/history/<item_id>` — public read, no auth required
- **Format**: Entries include `event_type`, `old_value`, `new_value`, `occurred` (ISO-8601 UTC)

## 🔧 Configuration

Everything lives in `config.yaml`:

```yaml
items:
  - Slack
  - Azure
  # ...your services

admin:
  user: admin

server:
  host: "0.0.0.0"
  port: 8920
```

**Production override:** set credentials via environment variables (preferred):

```bash
STATUS_ADMIN_USER=you
STATUS_ADMIN_PASS_HASH=scrypt:...   # generated above
./start.sh
```

### Environment variables

| Variable | Purpose |
|----------|---------|
| `STATUS_ADMIN_USER` | Override admin username from config.yaml |
| `STATUS_ADMIN_PASS_HASH` | Password hash (required for production — plaintext fallback is rejected) |
| `STATUS_SECRET_KEY` | Flask session encryption key (auto-generated per-session if unset) |
| `STATUS_NO_ARCHIVE=1` | Skip DB archival on restart (useful during development/testing) |

### Runtime persistence & backups

Admin changes (status, notes, reorder) are persisted back to `config.yaml` under a `_runtime` section so they survive DB resets. On every save:

1. Current `config.yaml` is rotated into `.bak1`
2. Existing backups shift up (`.bak1` → `.bak2` … → `.bak5`)
3. The new config is written atomically (`tempfile` + `os.replace`)

Backup files are excluded from git via `.gitignore`.

## 📁 Scripts & deployment

| Script | Purpose |
|--------|---------|
| `start.sh` | Launch development server (PID tracking, logs to file) |
| `stop.sh` | Clean shutdown via PID file |
| `restart.sh` | Kill + start without reinstalling deps |
| `rebuild.sh` | Full dep install + DB migrations + restart |
| `install.sh` | Systemd production deploy wizard |
| `cleanup.sh` | Manage `archives/`: `list`, `show <file>`, `prune [--keep N]`, `report` |

**Production install:** Copy the project directory, then `sudo ./install.sh [/opt/status-page]`:
- Provisions Python3 venv + dedicated `statuspage` user + systemd unit
- Runs Gunicorn behind `127.0.0.1:8920` on 2 workers
- Manages via `systemctl status/restart status-page`
- Credentials live in `/etc/status-page/env` (mode 0640)

## 🗂 File structure

```
status-my-page/
├── config.yaml              # Service names, admin creds, server cfg, runtime overrides
├── app.py                   # Flask routes + SQLite DB logic (history, archival, auth)
├── cleanup.sh               # Archive manager: list / show / prune / report
├── requirements.txt         # flask, pyyaml only!
├── static/
│   ├── css/style.css        # Dark theme, 3 breakpoints (≤640px, ≤425px)
│   └── js/app.js            # Vanilla JS: toggle, drag-drop, notes, history modal
├── templates/index.html     # Jinja2-rendered UI with login & history modals
├── start.sh / stop.sh / restart.sh / rebuild.sh / install.sh
├── tests/
│   ├── test_history.py      # Automated API/DB history test suite
│   └── test_health.sh       # Quick health-check script
├── License.md               # MIT © 2026 Simar Sahni
└── .venv/                   # (excluded from git/deploy)
```

## 🔒 Security

| Feature | Detail |
|---------|--------|
| Auth session | Flask signed sessions only — no plaintext admin cookie fallback |
| CSRF | per-request token, rotated on every successful mutation, 3-strike wipe |
| Login rate-limit | 5 attempts / IP before 30s lockout |
| Password hashing | werkzeug `scrypt` (timing-safe compare) |
| CSP header | `default-src 'self'`, inline CSS only (`'unsafe-inline'` on style-src) |
| Other headers | X-Content-Type-Options, X-Frame-Options=DENY, Referrer-Policy, Permissions-Policy |

## 💻 API endpoints

| Route | Method | Auth | Action |
|-------|--------|------|--------|
| `/` | `GET` | Public | Render full status page |
| `/api/history/<id>` | `GET` | Public | Return change timeline for a service |
| `/api/toggle/<id>` | `POST` | 🔒 Admin | Cycle: green → degraded → red |
| `/api/notes/<id>` | `POST` | 🔒 Admin | Save/update freeform note text |
| `/api/add` | `POST` | 🔒 Admin | Create new service item |
| `/api/delete/<id>` | `POST` | 🔒 Admin | Remove service + compact DB positions + prune history |
| `/api/rename/<id>` | `POST` | 🔒 Admin | Update service display name |
| `/api/reorder` | `POST` | 🔒 Admin | Apply drag-drop position map |
| `/api/csrf-token` | `GET` | 🔒 Admin | Fetch fresh CSRF token |
| `/login` / `/logout` / `/auth-check` | — | Public | Session management |

## 📜 License

MIT License. Copyright © 2026 Simar Sahni ([@notsimar](https://github.com/notsimar)). See [License.md](./License.md).
