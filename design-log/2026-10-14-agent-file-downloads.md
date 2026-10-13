# Agent File Downloads in Chat

**Date:** 2026-10-14
**Status:** accepted
**Author:** collaborative

## Context

The conversational agent ([2026-10-13-conversational-agent.md](2026-10-13-conversational-agent.md))
assumes Hermes collects data through our text-protocol tools (`extract_booths`), which
fill the conversation's `results` table that the Export CSV button downloads.

In production, for a large request ("extract every brand A–Z and #"), Hermes used the
Hermes Agent's **own** toolsets (browser, terminal, file write) instead. It produced a
better dataset than our tools could (1,980 listings, 585 exhibitor profiles, extra
columns such as showroom and market dates) and saved it on the Ubuntu worker machine:

```
File: /home/jomael/atlanta_apparel_brands_and_booths_A-to-hash.csv
This session didn't have a shared results table to update.
```

The user has no way to get that file: `results` stayed empty, so Export CSV has nothing
to export, and the path only exists on the worker machine.

Our own tools could not have done this job anyway: `MAX_URLS_PER_CALL=40`,
`AGENT_MAX_STEPS=12`, and a fixed `brand, booth` schema. So the fix is to deliver the
files Hermes creates, not to force Hermes back onto our tools.

## Decision

### 1. A known output folder per conversation

The worker creates `OUTPUT_ROOT/<conversationId>/` (default
`OUTPUT_ROOT=/home/jomael/booth-radar-outputs`, readable by both the Hermes user and the
worker service user) before each turn. The system prompt tells Hermes:

> When you produce a file for the user (CSV, XLSX, JSON, …), save it in `<dir>` and
> mention its file name in your reply. Files saved there are offered to the user as
> downloads in the chat.

The server already sends `conversationId`-scoped turns; the `chat` frame gains a
`conversationId` field so the worker can build the path.

### 2. Worker collects files after the turn

When `run_turn` finishes, the worker gathers candidate files from:

- new or modified files in the conversation's output folder (compared with a snapshot
  taken at turn start), and
- absolute paths mentioned in the reply, **only if** they resolve inside an allow-listed
  root (`OUTPUT_ROOT` and the Hermes home folder, via `ARTIFACT_ROOTS`). This catches
  files Hermes saved elsewhere despite the instruction, like the one above.

Filters: allowed extensions (`.csv .xlsx .json .txt .md`), max size `ARTIFACT_MAX_BYTES`
(default 10 MB), max 5 files per turn. Symlinks are resolved before the root check so
nothing outside the roots (for example `~/.ssh`, `.env`) can leak.

Each file is sent to the server as a `file` frame before `done`:

```
{ type: "file", turnId, payload: { name, mime, size, data: <base64> } }
```

### 3. Server stores files in MongoDB

Railway's disk is ephemeral, so files go in a `files` collection
(`{ _id, conversationId, name, mime, size, data: Binary, createdAt }`; 10 MB stays well
under the 16 MB document limit). The `file` event is relayed to SSE as
`{ id, name, size }`, never the bytes. On `done`, the assistant message is saved with
`attachments: [{ id, name, size }]`.

New endpoint:

```
GET /api/files/:id   → the file, with Content-Disposition: attachment
```

`GET /api/conversations/:id` returns `attachments` on each message.

### 4. Client shows download chips

`useStream` gets an `onFile` handler. `ChatBox` shows each attachment under the assistant
bubble as a chip (file icon, name, size, download button). Clicking it opens
`/api/files/:id`. Chips are restored on reload from the persisted message.

The existing results table and Export CSV button stay as they are for rows collected by
`extract_booths`.

## Alternatives Considered

- **Disable Hermes' native toolsets and force our tools.** This gives one data path, but
  our tools are limited to 40 URLs, 12 steps, and two columns. They would have failed
  this request.
- **Add an `import_csv {path}` tool that loads Hermes' file into `results`.** This keeps
  the single Export button, but the 2-column schema drops showroom/date columns and
  relies on Hermes to call the tool.
- **Serve files directly from the Ubuntu machine.** The worker is not publicly reachable
  by design. Exposing it adds attack surface.
- **Railway volume or object storage (S3/R2).** Handles bigger files, but adds a new
  dependency. MongoDB is already shared and is enough for files up to 10 MB.

## Consequences

- Large files travel worker → Railway over the existing WebSocket as base64 (~33 %
  overhead). The `ws` default max payload (100 MB) is fine for a 10 MB cap.
- The `files` collection grows over time. A TTL or cleanup job may be needed later.
- The allow-list is the security boundary. Hermes can be told by a chat user to write
  anything into an allowed root, so roots must not contain secrets (keep `worker/.env`
  outside them).
- The worker service user (root today) must be able to read `OUTPUT_ROOT`, and Hermes
  (runs as `jomael`) must be able to write to it.
- Requires deploying both the worker and the server; an old worker simply never sends
  `file` frames.
