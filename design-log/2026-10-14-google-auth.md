# Google OAuth Authentication

**Date:** 2026-10-14
**Status:** accepted
**Author:** collaborative

## Context

Booth Radar is an internal tool. Currently there is no authentication — anyone who can
reach the Railway URL can use the app. Adding Google OAuth gives us identity, access
control, and optional domain-based restriction (e.g. only `@yourcompany.com` accounts).

## Decision

### Stack

| Layer | Technology | Reason |
|-------|-----------|--------|
| Session storage | `express-session` + `connect-mongo` | Same MongoDB Atlas already in use; no extra infra |
| OAuth strategy | `passport` + `passport-google-oauth20` | De-facto standard; well-typed |
| Client auth state | Custom `useAuth` hook (no new routing lib) | App is simple; a hook + conditional render is sufficient |

### Flow

```
1. User visits /
   → useAuth calls GET /api/auth/me → 401
   → App renders <SignInPage />

2. User clicks "Sign in with Google"
   → Browser navigates to GET /api/auth/google
   → Passport redirects to Google's OAuth consent screen

3. Google redirects back to GET /api/auth/google/callback
   → Passport exchanges code for profile
   → Optional domain check (ALLOWED_EMAIL_DOMAINS env var)
   → Session created; user stored in session
   → Server redirects browser to /

4. App re-renders
   → useAuth calls GET /api/auth/me → 200 { user }
   → App renders <ChatBox user={user} />

5. Logout
   → POST /api/auth/logout
   → Session destroyed
   → useAuth re-fetches → 401
   → App renders <SignInPage />
```

### New server routes

| Route | Auth required | Purpose |
|-------|--------------|---------|
| `GET /api/auth/me` | No | Returns `{ user }` or 401 |
| `GET /api/auth/google` | No | Initiates OAuth flow |
| `GET /api/auth/google/callback` | No | Handles Google callback |
| `POST /api/auth/logout` | No | Destroys session |

All existing `/api/chat`, `/api/conversations`, `/api/greeting`, and `/api/stream`
routes are wrapped with `requireAuth` middleware.

### New env vars

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_CLIENT_ID` | Yes | OAuth client ID from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | Yes | OAuth client secret |
| `SESSION_SECRET` | Yes | Random string for signing session cookies |
| `BASE_URL` | Prod only | Canonical URL (e.g. `https://booth-radar.railway.app`) — used for the OAuth callback URL |
| `ALLOWED_EMAIL_DOMAINS` | No | Comma-separated domains (e.g. `yourco.com,partner.com`). If unset, any Google account can sign in |

### Client changes

- New `useAuth` hook — fetches `/api/auth/me` once on mount
- New `<SignInPage />` — rendered when unauthenticated; matches dark theme
- `App.tsx` — three render states: loading spinner, sign-in page, chat
- `ChatBox.tsx` — receives `user` prop; shows avatar + name + logout button at the bottom of the sidebar

## Alternatives Considered

- **Clerk / Auth0 / Firebase Auth** — Third-party hosted auth adds external dependencies and billing. Passport with Google OAuth is self-contained and free.
- **JWT instead of sessions** — JWTs are stateless but harder to invalidate. Sessions stored in MongoDB allow instant logout and revocation.
- **No auth** — Acceptable on day one (internal URL), but adding it now is straightforward and makes the tool production-ready.

## Consequences

- Requires a Google Cloud project and OAuth credentials (one-time setup; documented in `.env.example`).
- `BASE_URL` must be set correctly in Railway so the OAuth callback URL matches what is registered in Google Cloud Console.
- `SESSION_SECRET` must be a strong random string in production.
- Sessions expire after 7 days by default (configurable).
