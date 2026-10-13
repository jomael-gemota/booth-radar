# UX Enhancements v2 — Pinning, Centered Input, Auto-Title, Logo

**Date:** 2026-10-13
**Status:** accepted
**Author:** ai

## Context

Following the initial Claude-style redesign, five new UX improvements are requested
to bring the Booth Radar sidebar and chat experience closer to the ChatGPT interaction model.

## Decision

### 1. Pin Conversations
Add a `pinned` boolean to `ConversationSummary` (localStorage only). Pinned chats
are rendered in a dedicated "Pinned" section above all date groups with a visual
pin indicator. A pin/unpin icon appears on hover alongside the existing delete button.

### 2. Centered Input on New Chat
When no conversation is active (welcome state), the input box and greeting are
rendered together in a vertically-centered flex container (`welcome-center`),
matching ChatGPT's layout. Sending the first message switches back to the normal
bottom-anchored input layout with the messages scroll area.

### 3. Remove Sidebar Footer
The "Booth Radar · Internal Tool" footer text is removed from the sidebar to reduce
visual noise and give more room to the conversation list.

### 4. AI-Generated Conversation Titles
A new server endpoint `POST /api/conversations/:id/generate-title` calls GPT-4o-mini
to produce a 4–5 word title from the first user message. The client calls this endpoint
when `endTurn()` fires for the very first assistant turn of a new conversation, then
updates the stored title in localStorage and re-renders the sidebar.

### 5. Modern SVG Logo
The `📡` emoji is replaced everywhere with a custom `<RadarLogoIcon>` SVG component:
concentric rings + a sweep line + a blip dot. The favicon is also updated to match.

## Alternatives Considered

- **Client-side title truncation**: simple but produces ugly, unreadable titles.
  Rejected in favour of the AI approach since the OpenAI key is already present.
- **Persistent pin in MongoDB**: unnecessary complexity for an internal tool;
  localStorage is sufficient and avoids a DB schema migration.

## Consequences

- One new API route on the server (`generate-title`).
- The welcome layout JSX is restructured (conditional rendering, not duplicated — the
  input form is extracted to a shared inner render function `renderInputForm`).
- Minor CSS additions; no breaking changes to existing styles.
