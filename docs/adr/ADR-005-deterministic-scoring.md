# ADR-005: Deterministic, versioned rule-based scoring; no learned model in v1

**Status:** Accepted · **Date:** 2026-09-29 · **Deciders:** Product, chief estimator (to confirm defaults)

## Context

Estimators must trust and argue with the score. There are no labeled outcomes on day one. The
chief estimator's judgment is the best available model and changes with backlog and market.

## Decision

Score = weighted sum of six factors (type, size, GC, distance, timing, bid type) computed by pure
functions from a versioned JSON profile, followed by hard filters, caps, and boosts, producing an
ordered explanation. Profiles are edited by the chief estimator in the UI, immutable once
activated, and referenced by every score. Rescoring is idempotent.

## Options Considered

### Option A: Rule-based weighted score with explanation (chosen)
**Pros:** explainable, editable without a deploy, testable to the byte, works with zero history.
**Cons:** weights are opinions; will need tuning; cannot discover non-obvious interactions.

### Option B: LLM-judged fit score
**Pros:** zero configuration; reads nuance. **Cons:** non-deterministic, unexplainable in a way an estimator can adjust, drifts with prompt/model changes, and would make the digest's number feel arbitrary. Rejected for scoring; LLM output (type, scope, flags) remains an *input*.

### Option C: Logistic regression / gradient boosting on outcomes
**Pros:** learns real preferences. **Cons:** no data yet; small n forever (Ferry submits maybe 150 bids a year). Planned as a P2 *weight-tuning* aid, not a replacement: fit weights to outcomes, then show the chief estimator the suggested profile diff.

## Consequences

- Easier: calibration harness against the labeled corpus; profile diffs are readable.
- Harder: someone must own the profile; the UI must make that easy (SPEC-07).
- Revisit: after ≥ 200 recorded outcomes.

## Action Items
1. [x] `scoring` module: `score(snapshot, profile) -> ScoreResult`, default profile JSON, tests for boundaries and caps.
2. [ ] `make calibrate` over the labeled corpus.
