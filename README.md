<div align="center">
  <img src="client/public/logo.png" alt="Booth Radar Logo" width="96" />

  # Booth Radar

  An internal AI-powered tool that scrapes public websites — trade-show directories, product catalogs, dealer lists — for whatever details you ask for, through a conversational chat interface.

  ![Stack](https://img.shields.io/badge/stack-React%20%2B%20Node.js%20%2B%20Python-blue?style=flat-square)
  ![Deploy](https://img.shields.io/badge/deploy-Railway-8B5CF6?style=flat-square)
  ![Auth](https://img.shields.io/badge/auth-Google%20OAuth-EA4335?style=flat-square)
</div>

---

## Overview

Booth Radar lets you point it at a public website and say what you need — exhibitor brands and booths, product names, style numbers, specs, prices, dealer locations — and collects it into a results table, no manual copy-pasting required. The columns come from your request, and results are exported as a ready-to-use CSV file.

### Architecture

| Service | Technology | Where it runs |
|---------|-----------|---------------|
| **Client** | React + Vite (TypeScript) | Served by the server |
| **Server** | Express + SSE relay | Railway |
| **Worker** | Python agent — Playwright scraper + Hermes LLM + MCP server for Hermes | Ubuntu VM |

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

### Hermes MCP Setup

With `AGENT_TOOL_MODE=mcp`, Hermes calls Booth Radar's tools (`find_feeds`, `capture_api`, `read_feed`, `map_site`, `fetch_page`, `extract`, `navigate`, `snapshot`, `click`, `fill`, `select`, `scroll_until_end`, `paginate`, `start_job`, `cancel_job`, `extract_booths`, `clear_results`) natively through `worker/mcp_server.py` instead of using its own browser/terminal tools. Each call is forwarded to a tool bridge inside the worker (`127.0.0.1:TOOL_BRIDGE_PORT`), so the page cache, browser pool and live results table are shared. See [the design log entry](design-log/2026-11-07-general-web-scraping-mcp.md).

1. In `worker/.env`, set `TOOL_BRIDGE_TOKEN` (`openssl rand -hex 32`) and, for fast page extraction, `EXTRACT_BASE_URL` / `EXTRACT_MODEL` (free option: [Ollama](https://ollama.com) on the VM, e.g. `ollama pull qwen2.5:7b-instruct`).
2. Register the server in `~/.hermes/config.yaml` for the user Hermes runs as:

   ```yaml
   mcp_servers:
     booth-radar:
       command: /path/to/booth-radar/worker/.venv/bin/python
       args: ["/path/to/booth-radar/worker/mcp_server.py"]
       timeout: 900
       # Only needed if Hermes' user cannot read worker/.env:
       # env:
       #   TOOL_BRIDGE_TOKEN: "<same token as worker/.env>"
   ```

3. Limit the API server platform to Booth Radar's tools: run `hermes tools`, select the **api_server** platform and enable only `mcp-booth-radar` and `skills` (optionally `clarify`). Check with `hermes tools list`. To switch tools off on every platform instead, list them under `agent.disabled_toolsets` (e.g. `browser`, `terminal`, `code_execution`, `web`, `delegation`).
4. Let Hermes read Booth Radar's skills straight from the repo, so a `git pull` updates them. Add to `~/.hermes/config.yaml`:

   ```yaml
   skills:
     external_dirs:
       - /path/to/booth-radar/worker/hermes-skills
   ```

   `hermes skills list` should show `site-scraping`, `directory-crawl`, `interactive-pages` and `product-catalog` (category `booth-radar`). Check the files with `python test_local.py --skills`.
5. Restart Hermes, set `AGENT_TOOL_MODE=mcp` in `worker/.env`, and restart the worker. Its log shows `tool mode: mcp` and `[bridge] listening on …`.

**Skills:** the scraping strategy (which route to try first, field naming, directories, interactive pages, product catalogs) lives in `worker/hermes-skills/booth-radar/*/SKILL.md`. In mcp mode the system prompt keeps a short summary and Hermes loads the matching skill with `skill_view`; in inline mode the worker adds the skill files to the prompt. Hermes can edit skills it finds under `external_dirs`, so if `git status` on the VM shows changes under `worker/hermes-skills/`, review them and keep or revert (`git checkout -- worker/hermes-skills`). See [the design log entry](design-log/2026-11-12-hermes-skills.md).

To roll back, set `AGENT_TOOL_MODE=inline` and restart the worker.

### Scraping & Cache

The agent picks the fastest route for each site (see [the design log entry](design-log/2026-11-10-custom-fields-and-site-feeds.md)):

1. **Data feeds** — `find_feeds` detects WordPress REST content types and Shopify `products.json`; `read_feed` pulls every record (e.g. a 115-product catalog in ~7 s) and the extraction model maps them to the requested columns in parallel batches (`EXTRACT_CONCURRENCY`, default **6**; up to `MAX_FEED_RECORDS`, default **10000**).
2. **Captured APIs** — on sites without a known feed, `capture_api` opens a listing or search page in Chromium and records the JSON requests that return lists of records. Each becomes an `api:N` feed: `read_feed` replays it over plain HTTP and follows its page or offset parameter. An A–Z directory endpoint (a one-letter parameter such as `az=A`) is read for every letter and `#` in one call (`letters` limits it, e.g. `"A-C"`). On an item's page, `capture_api` also finds **detail lookup** endpoints (`/exhibitor/97991` → `OpenDetails?exhibitorIds=97991`); `read_feed` with `id_column` replays them for every ID in that results column, `LOOKUP_BATCH` (50) IDs per call when the endpoint takes several, up to `MAX_LOOKUP_IDS` (5000), and fills every row sharing an ID. Captured request headers stay in worker memory for `PAGE_CACHE_TTL` only. See [the design log entry](design-log/2026-11-12-filtered-and-lookup-feeds.md).
3. **Sitemaps** — `map_site` lists page URLs from the site's sitemaps.
4. **Pages** — `extract` reads each page and pulls the requested fields. Server-rendered pages are fetched over plain HTTP (`SCRAPE_HTTP_FIRST`, default on); JavaScript pages, or pages that yield no rows over HTTP, are rendered in Chromium. JSON-LD product data on the page is passed to the extractor. Pages longer than `MAX_PAGE_CHARS` are extracted in overlapping chunks.

5. **Interactive pages** — when data only appears after clicking, filtering, searching or scrolling, `navigate` opens the page in a browser session and lists its elements with refs (`e1`, `e2`…). `click`, `fill`, `select` and `scroll_until_end` act on those refs (a click the page blocks opens the link's address instead, or is forced), `snapshot` re-reads the page, `extract` with `session` reads the current page, and `paginate` clicks a "Next" control page by page while filling the table. A turn can hold up to 3 sessions; they close when the turn ends.

6. **Background jobs** — for more than `JOB_MIN_URLS` pages (default **100**), the agent calls `start_job` instead of `extract`: the pages (a URL list, the URLs in a results column, pages built from an ID column with `url_template` such as `https://site/exhibitor/{Exhibitor ID}`, or the site's sitemap; up to `MAX_JOB_URLS`, default **5000**) are extracted outside the chat turn, `JOB_BATCH_URLS` (40) at a time with at most `JOB_PAGE_CONCURRENCY` (4) tabs, one job at a time (`JOB_CONCURRENCY`). Rows keep appearing in the table, a chip above the message box shows progress with **Pause/Resume** and **Cancel**, and a finish message is added when the job ends. Chat stays open meanwhile; while a job runs, the table can't be cleared or replaced. Jobs are stored in MongoDB (`jobs` collection) and continue after a worker or server restart. See [the design log entry](design-log/2026-11-12-background-crawl-jobs.md).

Rows from later calls merge into earlier ones when their key columns match (e.g. a feed gives names and URLs, then product pages fill in style numbers).

**Polite and safe by default:** pages, feeds, APIs and sitemaps honour `robots.txt` (`RESPECT_ROBOTS`, default on), at most `DOMAIN_CONCURRENCY` requests (default **4**) go to one site at a time, and private, loopback and link-local addresses are refused, including on every redirect hop. See [the design log entry](design-log/2026-11-12-browser-sessions-and-navigation.md).

**Learned recipes** (`RECIPES_ENABLED`, default on): when `extract` gets 4 or more pages with the same URL shape, the first page goes to the extraction model, which also proposes CSS selectors; if they reproduce that page's rows, the other pages are read with the selectors and no model call. For feeds, the first batch's rows are matched to the record keys, and when every column maps to a key the remaining batches are filled in code. Recipes are saved in `worker/data/recipes.json` (`RECIPES_PATH`). A recipe that keeps failing is dropped and relearned; `refresh=true` ignores saved recipes. See [the design log entry](design-log/2026-11-11-api-capture-and-recipes.md).

- Pages are rendered in one warm Chromium with up to `SCRAPE_CONCURRENCY` tabs (default **6**). Pages from the same site share a browser context, and images, media, fonts and common trackers are blocked. A render waits for the page to go idle for at most `SCRAPE_IDLE_TIMEOUT_MS` (default **3000**), or only until a learned recipe's selector appears.
- Scraped pages, feeds, sitemaps and extracted rows are cached in worker memory for `PAGE_CACHE_TTL` seconds (default **3600 s**, up to `PAGE_CACHE_MAX` entries). Follow-up questions are answered without re-scraping.
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
.venv/bin/python test_local.py --feeds https://thorogoodusa.com           # list and read site data feeds
.venv/bin/python test_local.py --capture https://example.com/search --scroll   # record and read a page's JSON APIs
.venv/bin/python test_local.py https://example.com/item --fields "Name,Price,SKU"
.venv/bin/python test_local.py --skills                                     # check the Hermes skill files
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
