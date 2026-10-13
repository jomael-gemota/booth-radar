# AI-Generated Welcome Greeting

**Date:** 2026-10-13
**Status:** accepted
**Author:** collaborative

## Context

The welcome screen currently shows a static time-based greeting computed in the
browser (`getGreeting()`). The user wants it to feel dynamic and witty — more like
a personality than a form label.

## Decision

Add a server-side `GET /api/greeting?hour=N` endpoint that calls OpenAI
(`gpt-4o-mini`) to generate a short, clever greeting and one-sentence subtext
tailored to the user's local time-of-day period (morning / afternoon / evening /
night).

### Why server-side?

The `OPENAI_API_KEY` must never be exposed to the browser. All OpenAI calls go
through the Express server, which already holds secrets via environment variables.

### Caching

Results are cached **in memory** per period with a 45-minute TTL. This means:
- At most **4 calls per 45 minutes** across all active users (one per period).
- Cache survives server restarts only if the process stays alive (Railway's container
  usually does). A cold start simply makes one new call per period.

### Prompt design

```
System: You are the witty, charming personality of Booth Radar — an internal
tool that scrapes trade-show exhibitor directories to collect brand names and
booth locations. Your tone is sharp, warm, and professional.

User: The user is visiting the app in the {period}.
- greeting: max 7 words, time-aware, clever and warm. Avoid generic phrases.
- subtext: exactly 1 sentence, 15–20 words, playfully describe what Booth Radar does.
Respond only with JSON: {"greeting": "...", "subtext": "..."}
```

Temperature `0.9` keeps variety across cache refreshes.

### Fallback chain

```
OpenAI call succeeds  →  dynamic greeting shown with fade-in
OPENAI_API_KEY unset  →  endpoint returns static fallback JSON
Network / API error   →  endpoint returns static fallback JSON
Endpoint unreachable  →  client falls back to getGreeting() inline text
```

The UI always shows *something* — the dynamic layer is purely additive.

### Client UX

- The 📡 icon renders immediately (no change).
- Heading and subtext area is hidden (`opacity: 0`) while the fetch is in flight.
- On success (or client-side fallback), content fades in (`opacity: 1`, 400 ms ease).
- There is no spinner or skeleton — the icon alone is enough visual weight during
  the brief fetch window.

## Alternatives Considered

- **Stream the greeting token-by-token:** More impressive but adds SSE complexity for
  a cosmetic feature. Not worth it.
- **Cache in Redis/MongoDB:** Overkill for this usage pattern. In-memory is fine
  for a single-container Railway deployment.
- **Call OpenAI from the client directly:** Exposes the API key. Not acceptable.

## Consequences

- `OPENAI_API_KEY` must be added to the Railway server environment and to
  `server/.env.example` (without a real value).
- A new file `server/src/routes/greeting.ts` is added; `index.ts` mounts it.
- No new npm dependencies — Node 20's built-in `fetch` is used.
- If `OPENAI_API_KEY` is absent, the endpoint silently returns the static fallback
  so the welcome screen works even in environments without the key.
