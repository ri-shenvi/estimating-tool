# ADR-008: Layered dedupe — hard keys, soft-key evidence score, human review band

**Status:** Accepted · **Date:** 2026-09-29 · **Deciders:** Engineering lead, product

## Context

The same solicitation arrives through multiple channels and spawns follow-ups over weeks. False
merges (two projects shown as one) are worse than duplicates (one project shown twice) because a
false merge can hide a due date. Volume is small enough that a human can review ambiguous cases.

## Decision

Three layers, in order:
1. **Hard keys** (platform project id, email thread, GC + project number) auto-merge.
2. **Soft-key evidence score** from normalized name similarity, GC identity, geography, due-date
   proximity and owner; ≥ 0.9 auto-merge, 0.6–0.9 provisional attach + review queue, < 0.6 new.
3. **Human review** via the Needs review list with merge/split/undo; every merge logged.
An optional LLM tiebreak for the middle band exists behind a flag, default off.

## Options Considered

- **Pure LLM matching** ("is this the same project?"): flexible, but unexplainable and
  non-deterministic in the exact place where a wrong answer hides a deadline. Rejected as primary.
- **Exact-key only**: misses the channel-switch case (BuildingConnected invite, personal-email addendum), which is common. Rejected.
- **Embedding similarity**: helps with renames; adds a vector dependency for marginal gain at this volume. Deferred; `pg_trgm` suffices.

## Consequences

- Easier: every merge has a reason; undo is straightforward.
- Harder: thresholds need tuning on the corpus; the review queue must stay small (< 5/day) or it will be ignored.
- Revisit: if the review queue exceeds 10/day for two weeks, enable the LLM tiebreak and measure.

## Action Items
1. [x] `resolution` module with normalization, evidence scoring, tests for the cases in SPEC-03.
2. [ ] Threshold calibration on the labeled corpus.
