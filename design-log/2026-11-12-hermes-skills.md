# Hermes Skills for Scraping Strategy

**Date:** 2026-11-12
**Status:** accepted
**Author:** collaborative

## Context

Phase 7 of [general web scraping via MCP](2026-11-07-general-web-scraping-mcp.md) (§8).
The system prompt in `worker/agent.py` has grown with every phase: the "How to scrape
fast" ladder, field naming, interaction rules, and soon background jobs
([background crawl jobs](2026-11-12-background-crawl-jobs.md)). All of it is sent on
every turn, including plain chat turns that need none of it.

Hermes supports skills: a folder with a `SKILL.md` (YAML frontmatter `name`,
`description`, optional `metadata.hermes.requires_toolsets`) and markdown instructions.
The skill index (name and description) is part of Hermes' system prompt, and the agent
loads a skill's full text with `skill_view(name)` only when it needs it. Folders outside
`~/.hermes/skills/` can be added with `skills.external_dirs` in `~/.hermes/config.yaml`.

The general plan named `directory-crawl`, `site-recipe` and `price-comparison`.
`site-recipe` is no longer needed because recipes are learned in code, and
`price-comparison` waits for `shopping_search` (phase 5, not built).

## Decision

### 1. Skills in the repo

`worker/hermes-skills/booth-radar/<skill>/SKILL.md` (category `booth-radar`), each with
`requires_toolsets: [mcp-booth-radar]` so it only appears where the tools exist:

| Skill | When Hermes loads it | Contents |
|-------|---------------------|----------|
| `site-scraping` | Any new site or data request | The ladder (find_feeds → read_feed, capture_api, map_site / listing pages → extract, interactive pages, fetch_page), field naming, `key_fields`, filling details into feed rows, cache and `refresh`, when to use `start_job`. |
| `directory-crawl` | Exhibitor, dealer, store or member directories | Finding letter tabs, page numbers and categories; building the full URL list; `extract_booths` vs `extract`; large directories as a background job; reporting counts and failed pages. |
| `interactive-pages` | Data behind tabs, filters, search forms, "load more" or infinite scroll | Snapshots and refs, `fill` + `submit`, `select`, `scroll_until_end`, `paginate`, stale refs, 3 sessions per turn, preferring page URLs when they exist. |
| `product-catalog` | Product lists, specs and prices | Feed first, then product pages for specs; merging by URL or SKU; price and currency columns; comparing the same product across a few merchant sites with `extract` (until `shopping_search` exists). |

### 2. Registration on the VM

```yaml
# ~/.hermes/config.yaml
skills:
  external_dirs:
    - ~/booth-radar/worker/hermes-skills
```

A `git pull` updates the skills; nothing is copied. Hermes notes changed skills on the
next message of an open session. The `skills` toolset must be enabled for the
`api_server` platform (README step 3 already allows it).

### 3. Shorter system prompt

- **mcp mode:** the "How to scrape fast" section shrinks to a five-line summary of the
  ladder plus: "Before starting work on a website, load the matching booth-radar skill
  with skill_view." Persona, reporting rules and the results-table context stay in the
  prompt, because they apply to every turn.
- **inline mode:** Hermes' skills don't see Booth Radar's `<tool>` protocol, so the
  worker reads the same `SKILL.md` files (without frontmatter) and appends them to the
  system prompt. The files are the single source in both modes, and the hard-coded
  guideline text in `agent.py` is removed.
- `skills_list` and `skill_view` progress lines are hidden from the activity bubble,
  like `tool_search`.

### 4. Checking it works

`test_local.py --skills` checks that every `SKILL.md` parses, has `name`, `description`
and `requires_toolsets`, and only mentions tools that exist in `_TOOL_DOCS`. A live mcp
turn on the VM should show `skill_view` in the worker log before the first booth-radar
tool call.

## Alternatives Considered

- **Keep everything in the system prompt:** simplest and always followed, but every
  turn pays for all of it, and each phase makes it longer.
- **Copy skills into `~/.hermes/skills/`:** works without config, but copies drift from
  the repo after each pull.
- **Separate prompt text for each mode:** two copies of the same guidance would drift.

## Consequences

- Smaller prompt on every turn; strategy text can be edited without touching
  `agent.py`.
- One extra Hermes step (`skill_view`, about 1–3 s) on turns that scrape. Hermes may
  skip loading a skill; the five-line summary keeps the basic ladder in place when it
  does.
- Hermes' `skill_manage` can edit skills under `external_dirs` when the folder is
  writable, so an agent edit would show up as a change in the repo's `git status`. The
  README will say to review or revert such changes.
- New VM setup step (`skills.external_dirs`), documented in the README.
- Depends on phase 6 for the `start_job` guidance, so phase 7 ships after it.
