# UI Redesign — Claude-Style Two-Panel Layout

**Date:** 2026-10-13
**Status:** accepted
**Author:** collaborative

## Context

The current UI is a centered single-column layout with a top header and one chatbox
component. There is no persistent conversation history — only one conversation ID is
held in `localStorage`. Users cannot browse or revisit past sessions.

This entry supersedes the Chat UX section of
[`2026-10-13-booth-radar-architecture.md`](./2026-10-13-booth-radar-architecture.md).

## Decision

Redesign the entire client UI to match the professional two-panel layout popularised
by Claude, ChatGPT, and similar products:

### Layout

```
┌──────────────────────────────────────────────────────────────────┐
│ Sidebar (260px)          │ Main panel (flex-1)                   │
│ ──────────────────────   │ ──────────────────────────────────────│
│ 📡 Booth Radar      [←]  │ <current conversation title>         │
│ [+ New chat]             │ ──────────────────────────────────────│
│                          │                                       │
│ Today                    │  (messages scroll area)               │
│  • Scrape Atlanta…  [✕]  │                                       │
│  • Get booths A–C   [✕]  │                                       │
│                          │                                       │
│ Yesterday                │                                       │
│  • Atlanta full run [✕]  │ ──────────────────────────────────────│
│                          │ [input textarea]         [▶ send]     │
│ [Booth Radar v1]         │  Booth Radar can make mistakes.       │
└──────────────────────────┴───────────────────────────────────────┘
```

The sidebar is collapsible via a toggle button; when collapsed it shows only icons.

### Conversation history

- A second `localStorage` key (`booth-radar:conversations`) stores an array of
  `{ id, title, preview, createdAt }` objects (newest first).
- When the user sends the first message in a new session and the server returns a
  `conversationId`, a summary entry is created from the first user message text.
- Clicking a sidebar entry calls `GET /api/conversations/:id` and re-hydrates the
  message list and results table — exactly as the existing mount-time restore does.
- A ✕ button (visible on hover) deletes the local entry; if it was the active
  conversation, the view resets to "New chat".
- Conversations are grouped by date: Today / Yesterday / Previous 7 Days / Previous
  30 Days / Older.

### Message design

| Role        | Visual treatment                                              |
|-------------|---------------------------------------------------------------|
| `user`      | Right-aligned pill bubble, surface background                 |
| `assistant` | Left-aligned, no background, 📡 avatar, clean text             |
| `thinking`  | Collapsible panel with pulsing orb; auto-expands while streaming, collapses when done |
| `status`    | Italic small text with a pulsing dot, left-aligned            |
| `system`    | Centered warning banner (red tint)                            |

### Thinking / reasoning block

- A collapsible container with a purple-tinted border.
- Header row: `● Thinking…` (orb pulses while streaming) / `● View reasoning` (done).
- Body: slightly muted italic text with its own scroll area (max 320 px).
- Auto-expands while streaming, auto-collapses when the assistant's reply begins.

### Markdown rendering

Improved `renderMarkdown` handles:
- ` ``` ` fenced code blocks → `<pre class="code-block">`
- `` ` `` inline code
- `**bold**`
- Auto-linked URLs
- Line breaks (`\n` → `<br/>`)

### Input area

- Unified `<div class="input-wrapper">` contains the textarea and a circular Send
  icon button; the wrapper gains a focus ring on focus-within.
- A small disclaimer line sits below the wrapper ("Booth Radar can make mistakes.").
- Max-width 768 px, centred, to keep lines readable on wide screens.

## Alternatives Considered

- **Keep header, add a drawer for history:** Rejected — wastes vertical space and
  hides history behind an extra click. A persistent sidebar is the recognised pattern
  for chat tools and matches user expectation.
- **Fetch conversation list from server:** The server has no `GET /api/conversations`
  list endpoint. Storing summaries in `localStorage` is sufficient for a single-user
  internal tool and avoids a new API endpoint.

## Consequences

- `App.tsx` is simplified to a bare mount of `<ChatBox />`.
- `App.css` becomes nearly empty (layout is owned by ChatBox).
- `ChatBox.tsx` grows to own the full-page shell (sidebar + main panel).
- `index.css` is updated with new design tokens matching the dark theme.
- No backend changes required.
