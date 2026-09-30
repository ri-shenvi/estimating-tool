# SPEC-07: GC Directory and Scoring Profile Administration

**Status:** Draft · **Priority:** P0 · **Depends on:** SPEC-02, SPEC-06 · **Feeds:** SPEC-04

## Problem

GC relationship is the most decisive bid/no-bid factor and the one least visible in an ITB. The
tool needs a directory the chief estimator owns, that resolves the many ways a GC appears in email,
and that accumulates history.

## Goals

- 100% of opportunities from known GCs resolve to a directory record automatically once the GC has been seen twice.
- The chief estimator can change a tier, block a GC, or add a note in under a minute.
- GC statistics (invites, bids, submitted, won, hit rate, last award) are computed, not typed.

## Non-Goals

- Contact management beyond names/emails/roles observed in ITBs.
- Tracking GC payment history from accounting systems (manual notes only in v1; ERP integration P2).

## Functional Requirements

### F1. GC record

```
GC
  id, canonical_name, aliases[], domains[], kind (gc, cm, owner, developer, design_builder, platform)
  tier (A, B, C, D, blocked, unknown), tier_reason, tier_set_by, tier_set_at
  key_account (bool), notes (markdown), payment_notes
  contacts[] {name, email, phone, role, last_seen}
  offices[] {city, state}
  stats (computed): invites_12m, bids_12m, submitted_12m, won_12m, hit_rate_12m, invites_all, won_all, last_invite_at, last_award_at, avg_days_notice
  created_from (seed, extraction, manual)
```

### F2. Resolution

- On extraction, `gc_name` and contact domains are matched: exact domain → alias match (case/punctuation-insensitive) → trigram similarity ≥ 0.9 → otherwise a new GC with `tier=unknown`, `created_from=extraction`.
- Platform domains (`buildingconnected.com`, `procore.com`, `isqft.com`, `planhub.com`, `smartbidnet.com`) are never GC domains; they are stored on the `platform` kind records and excluded from matching.
- Generic email domains (gmail, outlook, yahoo) never create domain matches.
- New GCs appear in the digest's Needs review as "New GC seen: {name} — set tier" until a tier is set or `unknown` is confirmed.
- Merge tool for duplicate GCs (aliases and domains union; opportunities re-pointed; stats recomputed).

### F3. Seed list

Initial import (CSV) with the region's major GCs so day-one scoring is not all `unknown`:
PJ Dick, Rycon Construction, Mascaro Construction, Massaro Corporation, Landau Building Company,
Jendoco Construction, Burchick Construction, Volpatt Construction, Turner Construction (Pittsburgh),
Continental Building Company, Sota Construction Services, A. Martini & Co., dck worldwide, Franjo
Construction, Allegheny Construction Group, TEDCO Construction, Shannon Construction, Mosites
Construction, Nello Construction, Dick Building Company. All seeded with `tier=unknown`; the chief
estimator sets tiers during Phase 0.

### F4. Stats computation

- Recomputed on every opportunity status change and nightly.
- `invites` counts opportunities (not messages). `bids` = status ever reached `bidding`. `submitted`, `won` as recorded. `hit_rate = won / submitted` when `submitted ≥ 5`, else null (displayed "n/a (3 bids)").
- `avg_days_notice` = mean of (`bid_due` − `first_seen_at`) over 12 months; shown in the GC page and used in explanation text ("this GC usually gives 9 days").

### F5. GC page

- Header with tier control, key-account toggle, notes; stats tiles; opportunity list with status and outcome; contacts; aliases/domains editor; audit trail of tier changes.

### F6. Scoring profile editor

- Form for every profile element in SPEC-04 F1–F5: factor weights (sliders with sum indicator), project type table, size band, electrical share table, cost-per-SF table, distance curve breakpoints, timing table, bid type table, hard filters toggles, boosts, band thresholds, key accounts list.
- Preview panel: "Under this draft, of the last 30 days' 118 opportunities, 9 move up a band and 4 move down" with a list.
- Save as draft; activate with a note; view any previous version; diff two versions.
- Validation per SPEC-04 F7.

### F7. Territory and capacity settings

- Home base address (default 250 Curry Hollow Rd), distance method (straight-line × 1.3 in v1; drive-time API P1).
- Business days, holidays, digest time, recipients (SPEC-05).
- Estimator capacity (P1): hours/week per estimator, planned PTO.

## Acceptance Criteria

- [ ] Given an ITB from `jsmith@pjdick.com`, when resolved, then the GC is "PJ Dick" from the seed list.
- [ ] Given an ITB from "P.J. Dick Incorporated" with a gmail contact, when resolved, then alias matching maps it to PJ Dick and no new GC is created.
- [ ] Given an ITB from an unseen "Keystone Builders" at `keystonebuilders.com`, when resolved, then a new GC with `tier=unknown` exists and the digest asks for a tier.
- [ ] Given the chief sets Keystone to tier C with reason "slow pay on 2024 job", then the tier, reason, actor and timestamp are stored and all Keystone opportunities are rescored.
- [ ] Given 6 submitted bids to Mascaro with 2 wins, then `hit_rate_12m=0.33`; given 4 submitted, then hit rate displays "n/a (4 bids)".
- [ ] Given two GC records for the same firm are merged, then all opportunities point to the survivor, aliases and domains are unioned, and stats are recomputed.
- [ ] Given a draft profile that changes healthcare from 0.95 to 0.7, when previewed, then the panel lists opportunities whose band changes, and nothing is rescored until activation.

## Edge Cases and Required Tests

| Case | Expected | Test |
|---|---|---|
| GC with multiple domains (`pjdick.com`, `pjdick.net`) | Both resolve | `test_multi_domain` |
| Two unrelated GCs with similar names ("Continental Building Co" vs "Continental Construction") | Similarity below 0.9 or domain differs → separate | `test_similar_names_distinct` |
| Owner acting as GC (university facilities) | Kind `owner`; tier applies as GC tier | `test_owner_as_gc` |
| CM invites on behalf of owner on the key-account list | Owner boost applies; GC tier from CM | `test_cm_with_key_owner` |
| GC blocked while 2 opportunities are `bidding` | Existing `bidding` items keep status; flagged "GC blocked after decision"; new items score 0 | `test_block_with_active_bids` |
| Contact leaves GC A and appears at GC B | Contact recorded under both with `last_seen`; domain decides GC | `test_contact_moves` |
| Alias collision (alias already used by another GC) | Save rejected with pointer to conflict | `test_alias_collision` |
| Profile activated while nightly rescoring is running | Rescoring restarts with the new version; single final state | `test_profile_activation_race` |
| Stats window at exactly 365 days | Inclusive of day 365 | `test_stats_window_boundary` |
| Seed CSV re-imported | Idempotent; existing tiers not overwritten | `test_seed_idempotent` |
