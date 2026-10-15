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

### Scraping speed and cache

Pages are rendered on a pool of `SCRAPE_CONCURRENCY` threads (default 4), each keeping
a warm Chromium, with images, media and fonts blocked. `extract_booths` processes up to
`SCRAPE_CONCURRENCY` pages at once. Scraped pages and their extracted rows are cached in
worker memory for `PAGE_CACHE_TTL` seconds (default 3600, up to `PAGE_CACHE_MAX` entries),
so follow-up questions about a site are answered without re-scraping. The cache is
cleared when the worker restarts; set `PAGE_CACHE_TTL=0` to disable it.

### File downloads

Files Hermes creates during a turn (CSV, XLSX, JSON, TXT, MD, up to 10 MB, max 5 per
turn) appear as download chips under the reply. The worker gives each conversation a
folder at `OUTPUT_ROOT/<conversationId>/`, tells Hermes to save there, and also picks up
absolute paths mentioned in the reply if they are inside `ARTIFACT_ROOTS`. Hidden files
and folders (`.ssh`, `.env`, …) are never delivered. Files are stored in the MongoDB
`files` collection and served at `GET /api/files/:id`.

`OUTPUT_ROOT` must be writable by the user Hermes runs as (the worker creates the
folders with mode `777`). Do not add folders that contain secrets to `ARTIFACT_ROOTS`.

Try the agent in a terminal without the server:

```bash
.venv/bin/python test_local.py --chat
```
