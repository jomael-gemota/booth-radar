# User Isolation and Account Profiles

**Date:** 2026-10-14
**Status:** accepted
**Author:** collaborative

## Context

Conversations are currently stored in MongoDB without any owner association, and the
conversation list is tracked purely in `localStorage` (shared for any user on that
browser). With multiple Google accounts now possible, each user must only see and
access their own data.

## Decision

### Chat isolation

- Add `userId: string` (Google profile ID) to `ConversationDoc` and `FileDoc`.
- All conversation API queries filter by `{ ..., userId: req.user.id }` so a user
  can never access another user's data, even if they know the conversation ID.
- Add a `GET /api/conversations` endpoint that returns the current user's conversation
  list (title, preview, createdAt). This seeds the sidebar on first load and keeps
  multi-device sessions in sync.
- Add `title` and `preview` fields to `ConversationDoc` so the server can serve the
  sidebar list authoritatively. `title` is set by `generate-title`; `preview` is set
  from the first user message at conversation creation time.
- `localStorage` keys are scoped per user ID (`booth-radar:conversations:{userId}`)
  so logging in as a different user on the same browser starts fresh.

### User profiles

- New `userProfiles` collection in MongoDB keyed by Google user ID.
- Fields: `nickname`, `jobTitle`, `department`.
- `GET /api/me` — returns merged Google identity + stored customisations.
- `PATCH /api/me` — updates stored fields.
- `ChatBox` displays `profile.nickname ?? user.name` in the sidebar.
- Clicking the profile area opens a popover with "My Account" and "Sign out".
- "My Account" opens an `AccountModal` overlay with editable fields.

## Consequences

- Existing conversations in MongoDB (created before this change) have no `userId`
  and will not appear in any user's sidebar — effectively orphaned.
- The sidebar is now hydrated from the server on mount; `localStorage` is still used
  for the pinned flag (UX preference that does not need cross-device sync).
