# status-my-page

> 🌐 Personal service status dashboard with 3-state health indicators, mobile-responsive dark theme, and deploy scripts. View-only by default — admin login required to manage.

## ✨ Features

- **3-State Status System**: Click services to cycle 🟢 Operational → 🟡 Degraded → 🔴 Outage
- **Smart Notes**: Auto-show hidden notes only for degraded/outage states, auto-hide on green  
- **Dark Theme UI**: Responsive layout (≤640px & ≤425px breakpoints), mobile-first CSS with proper touch targets
- **Admin Controls**: Session-based auth with cookies, drag-and-drop reorder, inline rename, add/delete items, auto-saving notes
- **Config-Driven**: `config.yaml` controls everything — service names, credentials, server settings
- **Archive System**: Snapshots current state before every restart so history survives resets

## 🚀 Quick start

```bash
# Clone & setup
git clone https://github.com/notsimar/status-my-page.git
cd status-my-page

# Install dependencies  
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Edit your services in config.yaml → ./start.sh
open http://localhost:8920  # Default: admin/changeme
```

> **⚠️ Change the default password** in `config.yaml` or via env vars before exposing!

## 📱 Status states at a glance

| State | Emoji | Notes behavior |
|-------|-------|----------------|
| Operational | 🟢 Green | Hidden automatically |
| Degraded | 🟡 Yellow | Visible — admin can add context notes |
| Outage | 🔴 Red | Always visible with pulsing red glow |

*Click any service as admin to cycle through states ♪*

## 🔧 Configuration

Everything lives in `config.yaml`:

```yaml
items:
  - Slack
  - Azure
  # ...your services
  
admin:
  user: admin  
  password: changeme  # ← CHANGEME!
  
server:
  host: "0.0.0.0"
  port: 8920
```

**Production override:** `STATUS_ADMIN_USER=you STATUS_ADMIN_PASS_HASH=<hash> ./start.sh`

## 📁 Scripts & deployment

| Script | Purpose |
|--------|---------|
| `start.sh` | Launch server (ports/PID tracking, logs to file) |
| `stop.sh` | Clean shutdown via PID file    |
| `restart.sh` | Restart without reinstalling deps |  
| `rebuild.sh` | Full dep install + DB migrations + restart |
| `install.sh` | Systemd production deploy wizard      |

**Production install:** Copy `status-page/`, run `sudo ./install.sh [/custom/path]`:
- Provisions Python3 venv + dedicated user + systemd unit
- Manages via `systemctl status/restart status-page` 
- View logs: `journalctl -u status-page -f`

## 🗂 File structure

```
status-my-page/
├── config.yaml           # Service names, admin creds, server cfg  
├── app.py               # Flask routes + SQLite DB logic (3-state core)
├── archiver.py          # Startup archive saver for state history
├── cleanup.sh           # `list`/`show`/`prune` archives CLI wrapper 
├── requirements.txt     # flask, pyyaml only!
├── static/
│   ├── css/style.css    # Dark theme, 3 breakpoints (≤640px, ≤425px)...  
│   └── js/app.js        # Vanilla JS toggle cycle + drag-drop reorder logic
├── templates/index.html  # Jinja2-rendered UI w/ notes input fields & login modal
├── start.sh / stop.sh / restart.sh / rebuild.sh / install.sh
├── License.md           # MIT © 2026 Simar Sahni
└── .venv/               # (excluded from git/deploy)
```

## 💻 API endpoints (for internal use / custom integrations)

| Route | Method | Auth      | Action                                  |
|-------|--------|-----------|-----------------------------------------|
| `/`  | `GET`  | Public    | Render the full status page             |
| `/api/toggle/<id>` | `POST` | 🔒 Admin | Cycle: green → degraded → red           |  
| `/api/notes/<id>` | `POST` | 🔒 Admin | Save/update freeform note text          |
| `/api/add` | `POST` | 🔒 Admin  | Create new service item                 |
| `/api/delete/<id>` | `POST`| 🔒 Admin  | Remove service + compact DB positions   |
| `/api/rename/<id>` | `POST`| 🔒 Admin  | Update service display name             |
| `/api/reorder` | `POST` | 🔒 Admin  | Apply drag-drop position map            |
| `/login` / `/logout`/ `/auth-check` | — | Public | Session management   |

## 📜 License

MIT License. Copyright © 2026 Simar Sahni ([@notsimar](https://github.com/notsimar)). See [License.md](./License.md).
