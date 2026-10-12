# booth-radar

Chat with Booth Radar (backed by Hermes) to collect exhibitor brands and booth
locations from trade-show websites and export them as CSV.

- `client/` — Vite + React chat UI
- `server/` — Express API, SSE relay, WebSocket endpoint for the worker (Railway)
- `worker/` — Python agent: Hermes tool loop + Playwright scraper (Ubuntu)

See `design-log/` for architecture decisions.

## Checks

```bash
npm run check
```

Runs the server typecheck, then the client typecheck → lint → build.
Excludes the Python worker (no linter configured) and `npm audit`.

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
