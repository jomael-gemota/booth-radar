<div align="center">
  <img src="client/public/logo.png" alt="Booth Radar Logo" width="96" />

  # Booth Radar

  An internal AI-powered tool that scrapes trade-show exhibitor directories and collects brand names and booth numbers through a conversational chat interface.

  ![Stack](https://img.shields.io/badge/stack-React%20%2B%20Node.js%20%2B%20Python-blue?style=flat-square)
  ![Deploy](https://img.shields.io/badge/deploy-Railway-8B5CF6?style=flat-square)
  ![Auth](https://img.shields.io/badge/auth-Google%20OAuth-EA4335?style=flat-square)
</div>

---

## Overview

Booth Radar lets you point it at any trade-show exhibitor directory and automatically collects every brand name and booth number — no manual copy-pasting required. Results are exported as a ready-to-use CSV file.

### Architecture

| Service | Technology | Where it runs |
|---------|-----------|---------------|
| **Client** | React + Vite (TypeScript) | Served by the server |
| **Server** | Express + SSE relay | Railway |
| **Worker** | Python agent — Playwright scraper + Hermes LLM | Ubuntu VM |

```
client/       Vite + React chat UI
server/       Express API, SSE relay, WebSocket endpoint for the worker
worker/       Python agent: Hermes tool loop + Playwright scraper
design-log/   Architecture decision records
```

---

## Getting Started

### Prerequisites

- Node.js 20+
- Python 3.11+
- A running MongoDB instance
- Google OAuth credentials (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`)

### Local Development

Run both the server and client from the repo root:

```bash
npm run dev
```

This starts:
- **Server** on `http://localhost:3001` (tsx watch — auto-reloads on save)
- **Client** on `http://localhost:5173` (Vite HMR)

Start the Python worker separately:

```bash
cd worker
.venv/bin/python worker.py
```

> Set `SERVER_WS_URL=ws://localhost:3001/ws` in `worker/.env` for local dev.

---

## Worker Setup (Ubuntu)

```bash
cd worker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in HERMES_* and SERVER_WS_URL
```

Playwright browsers must be installed for the user the service runs as:

```bash
# For a systemd unit running as root
sudo .venv/bin/playwright install --with-deps chromium
```

### Scraping & Cache

- Pages are rendered on a pool of `SCRAPE_CONCURRENCY` threads (default **4**), each keeping a warm Chromium instance with images, media, and fonts blocked.
- Scraped pages and extracted rows are cached in worker memory for `PAGE_CACHE_TTL` seconds (default **3600 s**, up to `PAGE_CACHE_MAX` entries). Follow-up questions are answered without re-scraping.
- The cache clears on worker restart. Set `PAGE_CACHE_TTL=0` to disable it.

### File Downloads

Files created during a turn (CSV, XLSX, JSON, TXT, MD — up to 10 MB, max 5 per turn) appear as download chips below the reply.

- Each conversation gets a folder at `OUTPUT_ROOT/<conversationId>/`.
- Files are stored in MongoDB (`files` collection) and served at `GET /api/files/:id`.
- Hidden files and folders (`.ssh`, `.env`, …) are never delivered.
- `OUTPUT_ROOT` must be writable by the user Hermes runs as.

Test the agent in a terminal without the server:

```bash
.venv/bin/python test_local.py --chat
```

---

## Services & Deployment

### Server (Railway)

The server auto-deploys on every push to `main`.

To restart manually without a code change:

1. Open the [Railway dashboard](https://railway.app) → select the **booth-radar** service.
2. Click **⋮ → Restart** in the top-right deployment panel.

Or via the CLI:

```bash
railway redeploy
```

> Railway auto-restarts the server on crash (`restartPolicyType: on_failure`).

---

### Worker (Ubuntu systemd)

SSH into the Ubuntu machine, then use the following commands:

```bash
# Check status
sudo systemctl status booth-radar-worker

# Restart (picks up code/env changes)
sudo systemctl restart booth-radar-worker

# Stop / Start
sudo systemctl stop booth-radar-worker
sudo systemctl start booth-radar-worker

# View live logs
sudo journalctl -u booth-radar-worker -f
```

After pulling new code:

```bash
cd /path/to/booth-radar/worker
git pull
sudo systemctl restart booth-radar-worker
```

> If `worker/.env` was changed, a restart is required — systemd does not hot-reload env files.

---

## Quality Checks

```bash
npm run check
```

Runs in order: **server typecheck → client typecheck → lint → build**.

> Excludes the Python worker (no linter configured) and `npm audit`.

---

## Design Log

Architecture decisions and implementation notes live in [`design-log/`](./design-log/). Read these before making significant changes to understand prior context and rationale.
