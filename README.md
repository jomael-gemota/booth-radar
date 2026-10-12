# booth-radar

Chat with Booth Radar (backed by Hermes) to collect exhibitor brands and booth
locations from trade-show websites and export them as CSV.

- `client/` — Vite + React chat UI
- `server/` — Express API, SSE relay, WebSocket endpoint for the worker (Railway)
- `worker/` — Python agent: Hermes tool loop + Playwright scraper (Ubuntu)

See `design-log/` for architecture decisions.

---

## Services overview

| Service | Where it runs | How it starts |
|---------|--------------|---------------|
| **Server** (Express + SSE relay) | Railway | Auto-deploys on `git push main` |
| **Worker** (Python agent) | Ubuntu VM | `systemd` unit — manual restart |
| **Client** (React UI) | Served by the server | Rebuilt as part of a Railway deploy |

---

## Restarting services

### Server (Railway)

The server auto-restarts on every push to `main`.  
To restart it manually without a code change:

1. Open the [Railway dashboard](https://railway.app) → select the **booth-radar** service.
2. Click **⋮ → Restart** in the top-right deployment panel.

Or redeploy from the CLI:

```bash
railway redeploy
```

Railway will also auto-restart the server if it crashes (`restartPolicyType: on_failure`
in `railway.json`).

---

### Worker (Ubuntu systemd)

SSH into the Ubuntu machine, then:

```bash
# Check current status
sudo systemctl status booth-radar-worker

# Restart (picks up any code/env changes)
sudo systemctl restart booth-radar-worker

# Stop
sudo systemctl stop booth-radar-worker

# Start
sudo systemctl start booth-radar-worker

# View live logs
sudo journalctl -u booth-radar-worker -f
```

After pulling new code on the server you typically want to:

```bash
cd /path/to/booth-radar/worker
git pull
sudo systemctl restart booth-radar-worker
```

> **Note:** if `worker/.env` was changed, a restart is required for the new values
> to take effect — systemd does not hot-reload env files.

---

### Local development

Run both the server and client dev servers from the repo root:

```bash
npm run dev
```

This starts:
- `server/` on `http://localhost:3001` (tsx watch — auto-reloads on file save)
- `client/` on `http://localhost:5173` (Vite HMR)

The Python worker still needs to be started separately:

```bash
cd worker
.venv/bin/python worker.py
```

Point `SERVER_WS_URL` in `worker/.env` to `ws://localhost:3001/ws` for local dev.

---

## Checks

```bash
npm run check
```

Runs the server typecheck, then the client typecheck → lint → build.
Excludes the Python worker (no linter configured) and `npm audit`.

---

## Worker setup (Ubuntu)

```bash
cd worker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in HERMES_* and SERVER_WS_URL
```

Playwright browsers must be installed for the user the service runs as.
For a systemd unit without `User=` (runs as root):

```bash
sudo .venv/bin/playwright install --with-deps chromium
```

Try the agent in a terminal without the server:

```bash
.venv/bin/python test_local.py --chat
```
