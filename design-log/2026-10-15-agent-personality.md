# Cheerful Personality for the Booth Radar Agent

**Date:** 2026-10-15
**Status:** accepted
**Author:** collaborative

## Context

The chat agent ([2026-10-13-conversational-agent.md](2026-10-13-conversational-agent.md))
described itself only as "a friendly assistant", and its replies read flat. The team wants
Booth Radar to sound friendly, enthusiastic, joyful, and happy.

## Decision

Add a `Personality` section to `SYSTEM_PROMPT` in `worker/agent.py`: warm and upbeat,
celebrates finished scrapes, uses exclamation points now and then and at most one or two
emoji per reply, stays kind and honest when things fail. Enthusiasm must not override
accuracy, brevity, or the rule against mentioning paths and internal tools
([2026-10-15-hide-local-paths-in-replies.md](2026-10-15-hide-local-paths-in-replies.md)).

The extraction prompt in `worker/extractor.py` stays neutral because it must return strict
JSON. The welcome greeting (`server/src/routes/greeting.ts`) already has its own warm,
witty prompt.

## Alternatives Considered

- **Configurable persona via env var.** Not needed yet; one tone fits the team.

## Consequences

- Tone depends on Hermes following the prompt; it may vary slightly between replies.
- Requires redeploying the worker.
