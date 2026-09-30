# ADR-004: Claude structured outputs for classification and extraction

**Status:** Accepted · **Date:** 2026-09-29 · **Deciders:** Engineering lead, product

## Context

Inputs are free-form emails, HTML tables from platforms, and PDF letters. We need a typed record
with per-field confidence and source excerpts, ~200 times a day, with replayability for evals.
Trust depends on the estimator being able to see *where* a value came from.

## Decision

- One Messages API call per message using the Anthropic Python SDK's `messages.parse` with a
  Pydantic schema (`ExtractedOpportunity`), model `claude-opus-5-5` by default, adaptive thinking
  (SDK default), effort `medium`, server-side refusal fallbacks enabled (`fallbacks="default"`).
- Classification and extraction are the same call (kind is a field), with a cheap pre-filter
  (sender allow/deny lists, obvious newsletters) to skip LLM calls on clear `not_bid` mail.
- Deterministic post-processing enforces date rules, timezone normalization, GC canonicalization,
  and geocoding. The LLM never has the last word on a date.
- Prompt and schema are versioned together; every record stores model, prompt version, tokens.
- Fixture-driven eval harness gates prompt changes in CI.

## Options Considered

### Option A: LLM structured extraction with deterministic post-processing (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low-medium |
| Cost | ~$3–10/day at expected volume on Opus 5.5 |
| Scalability | Fine; batch API available if volume grows 50× |
| Team familiarity | High |

**Pros:** handles the long tail of formats; schema validity guaranteed by structured outputs;
source excerpts give explainability.
**Cons:** non-zero hallucination risk on dates (mitigated by post-processing and confidence rules); external dependency.

### Option B: Per-platform HTML/regex parsers + LLM only for free-form email
**Pros:** deterministic for platform mail. **Cons:** brittle to platform template changes; two code paths to test; platform mail is the *easy* part anyway. Kept as a P2 optimization for the pre-filter, not the extractor.

### Option C: Fine-tuned small model / classic NER
**Pros:** cheaper per call. **Cons:** needs thousands of labels; we have none on day one; worse on PDFs. Revisit after the corpus exists.

### Option D: Cheaper model (Sonnet 5.5 / Haiku 4.5) as default
**Pros:** lower cost. **Cons:** cost is already negligible at this volume; accuracy on due dates is the product. Model is a config value; the eval harness can justify a downgrade later.

## Consequences

- Easier: adding a field is a schema change plus fixtures.
- Harder: prompt regressions are silent without the eval harness; CI must run it.
- Revisit: when ≥ 1,000 labeled messages exist, evaluate a cheaper model or a hybrid.

## Action Items
1. [x] `extraction` module with `ClaudeExtractor` and `FakeExtractor` (fixtures) behind one protocol.
2. [x] Fixture format `.eml` + `.expected.json`; `make eval-extraction`.
3. [ ] Daily spend cap and pause (SPEC-09).
