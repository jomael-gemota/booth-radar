# User Logs Admin Page

**Date:** 2026-10-16
**Status:** accepted
**Author:** collaborative

## Context

The admin (jomael@outdoorequipped.com) wants a dedicated page to see all registered
users and whether they are currently online (actively using the site). The page
must be gated so only the admin can access it. It should appear as a new item in
the profile drop-down menu below "My Account".

## Decision

### Online Presence Tracking

Use a **last-seen timestamp** approach:

- `lastSeenAt: Date` is added to the `UserProfile` MongoDB document.
- The `requireAuth` middleware updates `lastSeenAt` in the background (fire-and-forget)
  on **every authenticated API request**, so any interaction refreshes the timestamp.
- A user is considered **Online** if their `lastSeenAt` is within the last **5 minutes**.
- The UserLogsPage polls `/api/admin/users` every **30 seconds** to keep the list fresh
  without a WebSocket connection.

### Access Control

The `/api/admin/users` endpoint checks `req.user!.email === 'jomael@outdoorequipped.com'`
and returns 403 for anyone else. The frontend menu item is likewise only rendered
for that email, so the page never even appears for other users.

### UI

- New component: `UserLogsPage` — styled to match `AccountPage` (same header/back-button
  pattern, same CSS variable palette).
- Table columns: Avatar · Name · Email · Job Title · Department · Status · Last Seen.
- Status badge: green "Online" pill or grey "Offline" label.
- Auto-refresh every 30 s; manual refresh button also available.
- New view value `'userlogs'` added to `ChatBox`'s view state.

## Alternatives Considered

- **WebSocket heartbeat per user** — more accurate but adds complexity; polling is
  sufficient for this internal tool.
- **Separate admin dashboard** — overkill; embedding the page inside the existing
  sidebar navigation keeps the UI consistent.

## Consequences

- MongoDB writes an extra `$set` on `lastSeenAt` on every authenticated request.
  This is cheap (indexed upsert) and acceptable for an internal tool.
- "Online" is approximate — a user who loaded the page but is idle will appear
  Online for up to 5 minutes after their last API call.
- Follow-up: could add a lightweight `/api/presence/ping` + client heartbeat for
  always-accurate presence if needed later.
