# Hide Local File Paths and Internal Notes in Agent Replies

**Date:** 2026-10-15
**Status:** accepted
**Author:** collaborative

## Context

[2026-10-14-agent-file-downloads.md](2026-10-14-agent-file-downloads.md) told Hermes to
"mention its full path in your reply" so the worker could find files saved outside the
conversation folder. Users now get those files as download chips, so the path in the
reply is noise and exposes the Ubuntu layout, e.g.:

```
/home/jomael/booth-radar-outputs/6acfad43e9b367c23dd00a9a/dallas-market-center-exhibitors-pages-1-10.csv
```

Replies also included internal notes such as "The shared results table wasn't updated;
its collection action wasn't available here." This happens when Hermes collects data
with its own toolsets instead of `extract_booths`: the prompt still asked it to remind
the user about the results table, so it explained why the table was empty.

## Decision

1. **Prompt.** Hermes mentions only the file name and says it is attached below as a
   download. It never mentions paths, folders, the server, its tools, or which actions
   were or weren't available. It mentions the results table / Export CSV only when it
   added rows with `extract_booths` in that turn. The finishing summary covers what was
   collected (site, row count, columns, pages covered, failures).
2. **Worker safety net.** `artifacts.hide_paths` replaces absolute local file paths
   (`/…/name.ext`, `~/…/name.ext`) with the file name. It is applied to streamed tokens
   (via `PathScrubber`, which holds back the trailing partial word) and to the `done`
   reply that the server persists. URLs are not touched.
3. `artifacts.collect` still scans the **raw** reply, so files Hermes saves outside the
   conversation folder are still delivered.

## Alternatives Considered

- **Prompt change only.** Simplest, but Hermes does not always follow instructions, and
  the path would still leak occasionally.
- **Scrub on the client.** Would leave paths in the stored conversation and in the
  history sent back to Hermes.

## Consequences

- Streamed text is delayed by at most one word.
- Paths containing spaces are not fully scrubbed (rare in Hermes output).
