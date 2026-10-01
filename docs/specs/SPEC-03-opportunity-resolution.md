# SPEC-03: Opportunity Resolution (Dedupe, Threading, Change Tracking)

**Status:** Implemented · **Priority:** P0 · **Depends on:** SPEC-02 · **Feeds:** SPEC-04, SPEC-05, SPEC-06

## Problem

One project generates a dozen messages across two channels over three weeks. Estimators need one
opportunity with a history, not twelve entries. Addenda and date changes must attach to the right
opportunity and produce a visible diff, because the two ways a sub loses a bid without pricing
badly are bidding the wrong addendum set and missing a moved date.

## Goals

- One opportunity per real-world solicitation: duplicate rate < 3%, false-merge rate < 1% on the labeled corpus.
- Every addendum, reminder, date change and RFI response is attached to its opportunity within one processing cycle.
- Every change to a tracked field is recorded as a diff with the message that caused it.

## Non-Goals

- Merging opportunities across *different* GCs bidding the same owner project into a "project" super-entity. Two GCs inviting Ferry on the same hospital are two opportunities (different bid dates, different relationships). A P2 "project" grouping may link them for awareness.

## Functional Requirements

### F1. Entities

```
Opportunity
  id, status (new, undecided, bidding, passed, snoozed, submitted, won, lost, cancelled, archived)
  canonical fields (same shape as ExtractedOpportunity, but merged)
  gc_id → GC (SPEC-07)
  first_seen_at, last_activity_at
  sources[] → Message (with role: origin, addendum, date_change, reminder, ...)
  addenda[] {number, received_at, message_id, summary}
  field_history[] {field, old, new, message_id, changed_at, auto: bool}
  fingerprint
  merge_log[]
```

### F2. Matching a new message to an existing opportunity

Candidates are generated cheaply, then confirmed:

1. **Hard keys** (auto-match, confidence 1.0):
   - Same platform project id (BuildingConnected/Procore project or bid-package id parsed from links).
   - Same email thread (`In-Reply-To`/`References` chain) as an opportunity's source message.
   - Same `project_number` and same GC domain.
2. **Soft keys** (candidate if any two agree):
   - Normalized project name similarity ≥ 0.85 (lowercase, strip punctuation, remove stopwords like "project", "new", "renovation", "bid package", "electrical").
   - Same GC (by id or domain).
   - Same city or geocode within 1 km.
   - Bid due dates within 3 days, or one side null.
   - Same owner name.
3. **Confirmation**: candidates with combined evidence score ≥ 0.9 auto-merge; between 0.6 and 0.9 the message is attached provisionally and the pair appears in the review page's "Possible duplicates" list; below 0.6 a new opportunity is created.
4. **Kind-specific rules**:
   - `addendum`, `date_change`, `reminder`, `prebid_notice`, `rfi_response`, `award` that match nothing create a *stub* opportunity with `status=new` and flag `orphan_update`, so a missing original is visible rather than dropped.
   - `itb` that matches an existing opportunity from a *different* GC is never merged; it creates a new opportunity linked as `related_project`.
   - `rfb` matching an earlier `itb` (or vice versa) for the same GC merges; `bid_type` history shows the change.

### F3. Field merging

- Canonical field = highest-confidence value across sources, except dates, which follow **latest message wins** among messages of kind `itb`, `addendum`, `date_change`, `reminder`, with the constraint that a `reminder` may only *confirm* a date (equal within the same day), never move it. A reminder that disagrees creates a `date_conflict` flag instead of a change.
- Estimator-edited fields (`locked_by_user`) are never overwritten automatically; a conflicting inbound value is recorded in `field_history` with `applied=false` and shown in the digest as "system saw a different value."
- `scope_items` and `flags` are unions across sources; removals only by an addendum whose `changes_described` states a removal (tracked as `removed_by_addendum`).
- Addenda numbering: gaps (Addendum 1 and 3 present, 2 missing) raise flag `addendum_gap` on the opportunity.

### F4. Change tracking

Tracked fields: `bid_due`, `prebid`, `rfi_deadline`, `scope_items`, `flags`, `bid_type`,
`size_signals`, `addenda`, `status`. Every change writes `field_history` and marks the
opportunity `changed_since_digest=true` with a human-readable `change_summary` (e.g., "Due date
moved Oct 16 → Oct 21 (Addendum 2)").

### F5. Status lifecycle

```
new → undecided | bidding | passed | snoozed
undecided → bidding | passed | snoozed
snoozed → (auto at snooze_until) undecided
bidding → submitted | passed | cancelled
submitted → won | lost | cancelled
any → archived (manual or 180 days after last activity with no bid)
cancelled ← award kind with cancellation language, any status
```

`passed` opportunities still receive updates (so a re-scoped job can be reconsidered) but are
excluded from digest sections other than a one-line "Passed job changed materially" notice when
`bid_due` moves by > 7 days or `size_signals` change by > 50%.

### F6. Manual merge and split

Review page supports: merge two opportunities (choose survivor; all sources and history move;
merge_log records it), split a source message out into a new opportunity, and undo either within
30 days.

## Acceptance Criteria

- [x] Given a BuildingConnected invite and a direct GC email for the same project name and GC arriving 2 hours apart, when both are processed, then one opportunity exists with two sources and `delivery_channel` shows both.
- [x] Given an opportunity with `bid_due` Oct 16 and a subsequent `date_change` message saying Oct 21, when processed, then `bid_due` is Oct 21, `field_history` has one entry citing the message, and `change_summary` reads "Due date moved Oct 16 → Oct 21."
- [x] Given a `reminder` saying "bids due 10/17" for an opportunity whose due date is Oct 16, when processed, then `bid_due` stays Oct 16 and flag `date_conflict` is set with both values.
- [x] Given Addendum 3 arrives and Addendum 2 was never seen, when processed, then `addendum_gap=true` and the digest shows "Addendum 2 not received."
- [x] Given two different GCs each invite Ferry to "UPMC Passavant ED Expansion", when processed, then two opportunities exist, each linked to the other as `related_project`.
- [x] Given an estimator has locked `bid_due` to Oct 16 and a message arrives saying Oct 15, when processed, then `bid_due` remains Oct 16, `field_history` records the unapplied value, and the digest shows the discrepancy.
- [x] Given an `addendum` message matching no opportunity, when processed, then a stub opportunity is created with `orphan_update=true` and appears in Needs review.
- [x] Given two opportunities merged manually then undone, when viewed, then sources, history and status are restored exactly.

## Edge Cases and Required Tests

| Case | Expected | Test |
|---|---|---|
| Same project name, same GC, due dates 60 days apart | Two opportunities (rebid / phase 2); linked `related_project` | `test_rebid_not_merged` |
| Project renamed between ITB ("Bldg 3 Fitout") and addendum ("Building Three Tenant Improvement") | Merged via thread/platform id or GC + city + date; if only name available, lands in Possible duplicates | `test_rename_soft_match` |
| GC sends ITB from BuildingConnected, then addendum from personal email with no link | Matched on GC + name similarity + date window | `test_channel_switch` |
| Generic project name ("Office Renovation") from same GC twice in a month, different addresses | Two opportunities; geocode distance > 1 km breaks the tie | `test_generic_name_distinct_sites` |
| Reply-all chatter in the thread from Ferry's own estimator | Attached as source with role `internal`, no field changes | `test_internal_reply` |
| Date change email that also includes Addendum 1 | Both a `field_history` entry and an addendum record from one message | `test_multi_effect_message` |
| Addendum received twice (CC fan-out) | One addendum record; `copies=2` | `test_duplicate_addendum` |
| Addendum numbering restarts ("Addendum A", "Bulletin 1") | Non-numeric labels kept verbatim; gap detection disabled for that opportunity | `test_nonnumeric_addenda` |
| Due date changes 4 times | History has 4 entries; digest shows only latest and the count "(4 changes)" | `test_many_date_changes` |
| `award` message "Thank you for bidding; awarded to another firm" | Status `lost`; outcome recorded with source; estimator can correct | `test_regret_sets_lost` |
| Cancellation for an opportunity we `passed` | Status `cancelled`; no digest noise beyond the Changed section one-liner | `test_cancel_passed` |
| Snoozed opportunity's due date arrives before snooze_until | Snooze breaks early; opportunity returns to Needs decision with flag `snooze_overridden` | `test_snooze_vs_due` |
| Merge of two opportunities that each have a different decision | Merge blocked with an error explaining the conflict; user must resolve | `test_merge_conflicting_decisions` |
| 500 opportunities in the candidate window | Matching completes in < 2 s per message | `test_matching_performance` |
| Message whose thread references an archived opportunity | Opportunity un-archived, flagged `reactivated` | `test_reactivate_archived` |

## Implementation Notes

Where each part lives:

| Part | Code |
|---|---|
| Normalization, fingerprint, geohash | `resolution/normalize.py` |
| Hard keys, soft-key evidence score, `decide()` | `resolution/evidence.py` |
| Date / scope / size merge rules, award language, gap detection | `resolution/merge.py` (pure) |
| Candidate generation, field merging, change tracking, status effects | `worker/pipeline.py` |
| Manual merge, split, undo | `worker/curation.py`, `POST /api/opportunities/{id}/merge` \| `/split`, `POST /api/merges/{id}/undo` |
| Review queue, possible duplicates, sources, undo list | `web/routers/opportunities.py`, `opportunities.html`, `opportunity.html` |
| Schema | `opportunity_keys`, `merge_log`, `addenda.copies`, `opportunities.geohash`, `opportunities.material_change` (migration `0003`) |

Three decisions the spec left open, resolved while implementing:

* **Two geocoded sites more than 1 km apart score below 0.6, not below 0.9.** The edge-case table
  requires two opportunities for the distinct-sites case, and the 0.6–0.9 band attaches the message
  provisionally, which is one opportunity. ADR-008 prefers a duplicate to a false merge, so the
  distance test drops the pair out of the band entirely.
* **An update's due date cannot count against a match.** An `addendum` or `date_change` exists in
  order to move the date, so a date that disagrees is floored at "no evidence" rather than scored
  zero; a date that agrees still counts for the match, and still separates two rebids.
* **Ties break on the date the message itself states.** Two rebids of one project score identically
  on name, GC and geography, so without a tiebreak an addendum would attach to whichever row the
  database returned first.

An outcome the model read out of an award email is correctable: `won ↔ lost ↔ submitted` were added
to the lifecycle in `decisions/state.py`, because the edge-case table requires it and the F5 diagram
does not allow it.

Platform ids are namespaced and package-scoped. Procore's first path segment is the GC's *company*
id — `app.procore.com/2318842/...` is identical for every job that GC posts — so it is skipped and
the bid-package id further down the path is taken instead; a bid-package id beats a project id when
a link carries both. These become hard keys that merge at confidence 1.0, so a value shared across a
GC's whole portfolio is a guaranteed false merge.

An award notice is never dropped. `won` stays unreachable without `submitted` — we cannot win what
we never bid — so an award email claiming otherwise is recorded as an unapplied `status` entry with
an `outcome_conflict` flag rather than applied or ignored. `lost` and `cancelled` widen instead,
because F5 spells out "any status" for cancellation and the edge-case table sets `lost` from a
regret letter without conditioning on what we had decided.

The trigram pre-filter uses the `%` operator, not `similarity(a, b) > x`. `gin_trgm_ops` indexes
the operator; a function call in the predicate is not indexable at all, so the function form leaves
the GIN index costing writes and serving no reads. `%` takes its threshold from
`pg_trgm.similarity_threshold`, which candidate generation sets per transaction with `SET LOCAL` so
nothing leaks across a pooled connection. `make test-postgres` asserts the execution plan, because
the two forms return identical rows and only `EXPLAIN` tells them apart.

## Technical Notes

- Fingerprint = sha1(normalized_name | gc_domain | city | due_week). Used only as a fast pre-filter; never as the sole merge criterion.
- Candidate generation via SQL (trigram similarity on `normalized_name`, GC id, geohash prefix); confirmation in code; optional LLM tiebreak only for the 0.6–0.9 band and only when auto-review is enabled (default off, so that merges stay explainable).
