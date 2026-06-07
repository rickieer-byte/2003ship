# Deploying ship2003 on PythonAnywhere

Replace `YOURUSERNAME` with your PythonAnywhere account name throughout.

## 1. Upload the project

**Git (recommended)**

```bash
cd ~
git clone https://github.com/YOURORG/ship2003.git
```

**Or** upload a zip and unzip into `~/ship2003`.

Project path on PA: `/home/YOURUSERNAME/ship2003`

## 2. Virtualenv and dependencies

In a **Bash console**:

```bash
cd ~/ship2003
mkvirtualenv --python=/usr/bin/python3.10 ship2003-env
pip install -r requirements.txt
```

If `mysqlclient` fails to install, try again on a paid plan or check PA’s MySQL client docs for your account tier.

## 3. MySQL database

1. Open the **Databases** tab and set a MySQL password (save it).
2. Create a database (PA shows the full name, e.g. `YOURUSERNAME$escalation_db`).
3. Create `.env` in the project root (copy from `.env.example`):

```env
FLASK_SECRET_KEY=your-long-random-secret-at-least-32-chars

DB_HOST=YOURUSERNAME.mysql.pythonanywhere-services.com
DB_USER=YOURUSERNAME
DB_PASSWORD='your-pa-mysql-password'
DB_NAME=YOURUSERNAME$escalation_db

SIMULATION_MODE=true
GEOFENCE_RADIUS_KM=2.0
```

**Note:** If the password contains `#`, wrap it in **single quotes** in `.env`.

Optional alerts:

```env
SLACK_WEBHOOK_URL=
SMTP_HOST=
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USER=
SMTP_PASSWORD=
ALERT_EMAIL_FROM=alerts@yourdomain.com
ALERT_EMAIL_TO=ops@yourdomain.com
```

## 4. Load schema and seed data

```bash
workon ship2003-env
cd ~/ship2003
python db/setup.py
```

Expected output: `Database "YOURUSERNAME$escalation_db" is ready.`

## 5. Web app configuration

**Web** tab → **Add a new web app** → Manual configuration → Python 3.10.

| Setting | Value |
|---------|--------|
| Source code | `/home/YOURUSERNAME/ship2003` |
| Virtualenv | `/home/YOURUSERNAME/.virtualenvs/ship2003-env` |
| WSGI file | `/home/YOURUSERNAME/ship2003/wsgi.py` |

`wsgi.py` already exposes `application` for PA — no edits needed unless you rename the project folder.

Click **Reload** after any change.

## 6. Simulation (demo GPS + vessel movement)

### Option A — Scheduled task (recommended for demos)

**Tasks** tab, every 5 minutes:

```bash
/home/YOURUSERNAME/.virtualenvs/ship2003-env/bin/python /home/YOURUSERNAME/ship2003/cron/run_simulation_tick.py
```

### Option B — Lazy ticks (no cron)

With `SIMULATION_MODE=true`, simulation advances when users open the **dashboard** or **live map** (`/tracking`). Sufficient for manual demos on tiers without scheduled tasks.

Fleet Manager can also trigger a tick while logged in via `POST /api/simulation/tick`.

## 7. Smoke test

Open `https://YOURUSERNAME.pythonanywhere.com/login`

| Account | Password | Role |
|---------|----------|------|
| `planner_user` | `password123` | Planner |
| `dispatcher_user` | `password123` | Dispatcher |
| `manager_user` | `password123` | Fleet Manager |

Driver app: `https://YOURUSERNAME.pythonanywhere.com/driver`

| Driver | Phone tail |
|--------|------------|
| Mubarak Ali | `4567` |
| Siti Aisha | `5432` |
| Marcus Chen | `9876` |
| Rohan Raj | `2345` |

Verify:

- Dashboard loads containers
- **Instant Allocate** on a pending container
- `/tracking` shows driver markers
- `/replay` shows event history

Automated local checks (run against a running app):

```bash
python scripts/pa_walkthrough.py
python scripts/e2e_dispatch_flow.py
```

## 8. Troubleshooting

| Problem | Fix |
|---------|-----|
| 500 on every page | Check **Web → Error log**; usually bad `.env` or DB credentials |
| DB connection refused | Use `YOURUSERNAME.mysql.pythonanywhere-services.com`, not `localhost` |
| Login fails | Re-run `python db/setup.py` (seed hashes must match current Werkzeug) |
| Map/drivers not moving | Set `SIMULATION_MODE=true` or add the scheduled task |
| JWT warnings | Use a `FLASK_SECRET_KEY` of at least 32 characters |

## 9. Before sharing the demo URL

- Change default seed passwords or create real accounts
- Use a strong `FLASK_SECRET_KEY`
- Production uses `wsgi.py` only — do not run `python app.py` on PA

## Quick reference

```bash
workon ship2003-env
cd ~/ship2003
pip install -r requirements.txt
cp .env.example .env   # edit with PA MySQL credentials
python db/setup.py
# Web tab: WSGI → wsgi.py, set virtualenv, Reload
```
