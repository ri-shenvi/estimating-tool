# SPEC-04: Fit Scoring

**Status:** Draft · **Priority:** P0 · **Depends on:** SPEC-02, SPEC-03, SPEC-07 · **Feeds:** SPEC-05

## Problem

Estimators decide bid/pass on GC, building type, size, distance, timing and red flags. Today that
judgment lives in the chief estimator's head and is applied inconsistently under inbox pressure.
The tool must apply it consistently, explain itself, and be adjustable by the chief estimator.

## Goals

- Score every opportunity 0–100 with a factor breakdown an estimator can read in five seconds.
- Deterministic: same inputs + same profile version = same score.
- Editable by the chief estimator without a deploy; every profile change is versioned and attributable.
- On the labeled corpus, ≥ 80% precision and ≥ 85% recall for "bid" at the configured threshold.

## Non-Goals

- Machine-learned weights (P2, after ≥ 200 outcomes).
- Predicting win probability or margin. Fit is "should we spend estimating hours," not "will we win."

## Functional Requirements

### F1. Score structure

```
fit_score = clamp( Σ_f weight_f × factor_f(0..1) × 100 ) then apply hard filters and caps
```

Factors and defaults (weights sum to 1.0):

| Factor | Default weight | Inputs | Default function |
|---|---|---|---|
| `project_type` | 0.25 | `project_type`, `project_subtype`, `trade_relevance` | Lookup table per type: e.g. higher_education 1.0, healthcare 0.95, commercial_office 0.9, high_tech_research_data_center 0.9, religious 0.85, light_industrial_utility 0.8, multifamily_hotel_mixed_use 0.6, k12_education 0.6, retail_restaurant 0.5, government_civic 0.6, parking_transportation 0.4, heavy_industrial 0.2, residential_single_family 0.0, site_civil_only 0.1, other 0.4, unknown 0.5. `trade_relevance=partial` multiplies by 0.6; `none` sets the factor to 0 and triggers the hard cap below. |
| `size` | 0.25 | estimated electrical value (see F2) | Trapezoid: 0 below `min_floor`, ramps to 1.0 across `sweet_low..sweet_high`, ramps down to 0.2 at `max_ceiling`, 0 above `hard_max`. Defaults: floor $75K, sweet $250K–$4M, ceiling $8M, hard max $15M. Unknown size → 0.5 with reason "size unknown". |
| `gc` | 0.25 | GC tier, GC stats (SPEC-07) | Tier A 1.0, B 0.8, C 0.5, D 0.2, blocked 0 (hard filter), unknown 0.5. Adjusted ±0.1 by historical hit rate when ≥ 5 bids: hit rate ≥ 25% +0.1; < 5% −0.1. Owner-direct (Separations Act prime) uses the owner's tier if present, else 0.6. |
| `distance` | 0.10 | drive distance from 250 Curry Hollow Rd | ≤ 25 mi 1.0; 25–50 linear to 0.7; 50–90 linear to 0.3; > 90 mi 0.1; unknown 0.6. |
| `timing` | 0.10 | days until `bid_due`, count of `bidding` opportunities due within ±3 days, days to `prebid`, estimator capacity (P1) | Days-to-due: < 3 days 0.2, 3–6 0.6, 7–21 1.0, 22–45 0.9, > 45 0.7, unknown 0.5. Multiply by congestion factor: 1.0 if ≤ 1 other bid due that week, 0.8 if 2, 0.6 if ≥ 3. Mandatory pre-bid already passed → factor 0 and hard filter. |
| `bid_type` | 0.05 | `bid_type`, `sector` | negotiated/design_assist 1.0, design_build 0.9, gmp/budget 0.7, hard_bid private 0.7, hard_bid public 0.6, unknown 0.7. |

### F2. Estimated electrical value

Order of precedence:
1. `stated_electrical_value`.
2. `stated_project_value × electrical_share[project_type]`, defaults: data center 0.30, healthcare 0.15, higher_ed 0.13, high_tech_research 0.18, commercial_office 0.11, k12 0.11, religious 0.10, multifamily 0.08, retail 0.08, light_industrial 0.10, heavy_industrial 0.12, parking 0.07, other 0.10.
3. `square_feet × cost_per_sf[project_type] × electrical_share`, with defaults for cost per SF by type (editable).
4. Unknown.

The estimate, its method, and its inputs are shown in the explanation ("~$1.6M electrical, from
$12M project value × 13% higher-ed share").

### F3. Hard filters and caps (applied after weighting)

| Condition | Effect | Reason text |
|---|---|---|
| GC tier `blocked` | score = 0 | "GC is on the do-not-bid list" |
| `trade_relevance=none` | score capped at 5 | "No electrical scope" |
| `open_shop_indicated` flag | score capped at 20 | "Open-shop pricing indicated" |
| Mandatory pre-bid already passed at scoring time | score capped at 10 | "Mandatory pre-bid on {date} already passed" |
| `bid_due` already passed | score capped at 5 | "Bid due date has passed" |
| Estimated electrical value > `hard_max` | score capped at 15 | "Above maximum size" |
| `residential_single_family` | score capped at 5 | "Residential" |
| `past_due_at_receipt` with low confidence | no cap, but reason "Due date uncertain, verify" | |

### F4. Boosts (additive, capped so total ≤ 100)

| Condition | Boost |
|---|---|
| scope includes `solar_pv`, `battery_storage`, or `ev_charging` | +5 |
| owner on `key_accounts` list | +8 |
| `design_build_engineering` or `bim_coordination` in scope | +3 |
| LEED / sustainability language in scope_text | +2 |
| GC explicitly requests Ferry by name in body ("we'd like Ferry to bid") | +5 |

### F5. Recommendation band

| Score | Band | Digest treatment |
|---|---|---|
| ≥ 70 | **Bid** | Top section, expanded |
| 45–69 | **Consider** | Listed, collapsed |
| 20–44 | **Likely pass** | One line each |
| < 20 | **Pass** | Count only, with link |

Thresholds are part of the profile.

### F6. Explanation

Every score stores `explanation[]`: ordered list of `{factor, weight, value, contribution, reason}`
plus `caps[]`, `boosts[]`, `missing_inputs[]`. The digest renders the top three positive and the
top negative contributions in plain language.

"Positive" means the factor is *helping* — not merely that it is one of the largest numbers. The
two lists must be disjoint: a factor that appears as a reason to bid cannot also appear as the
reason not to. A factor at or below its neutral value is not a reason for anything and belongs in
neither list; if fewer than three factors clear that bar, render fewer than three.

**Known deviation (found 2026-09-30 against the running product, unfixed).** `ScoreResult.top_positive`
is `sorted(contributions, key=-contribution)[:3]` with no floor, while `top_negative` ranks by
shortfall from the maximum (`contribution - weight * 100`) and filters to `value < 0.7`. On a
weighted-average score, contribution is dominated by *weight*, so the "positive" list is really
"the three heaviest factors" regardless of merit, and a mid-valued heavy factor satisfies both
definitions at once. Every one of the 22 digest items in a fixture-corpus run carried a
self-contradiction:

```
Why: government civic (+), size unknown (+), GC tier not set (+); size unknown (-)

factor          weight   value  contrib
project_type      0.25    0.60     15.0
size              0.25    0.50     12.5   <- top_positive #2 AND top_negative #1
gc                0.25    0.50     12.5
timing            0.10    0.70      7.0
```

Nothing about that job scores above 0.60, yet three facts are presented as reasons to bid, and
"size unknown" — pure absence of information — is presented as the second-best one. This is the
chief estimator's trust story (`G3`, and "show me *why* it scored as it did so that I can trust or
override the number") failing on every row.

### F7. Scoring profile

- Stored as a versioned document (`scoring_profile` table: version, json, author, created_at, note, active flag).
- Edited in the admin UI as a form (weights, type table, size band, distance curve, thresholds, boosts) with a live preview that rescores the last 30 days and shows how many opportunities change band.
- Activating a version rescores all non-archived opportunities and marks changed bands in the next digest ("Rescored under profile v7").
- Weight validation: sum to 1.0 ± 0.001; all values within stated ranges; at least one type with factor 1.0.

### F8. Rescoring triggers

Any change to: canonical opportunity fields, GC tier or stats, profile version, or the passage of
time crossing a timing boundary (nightly rescoring pass before the digest).

## Acceptance Criteria

- [ ] Given a higher-education, $12M project, GC tier A, 18 miles away, due in 14 days, hard bid private, when scored under the default profile, then score is in the Bid band and explanation lists project type, size (~$1.56M), and GC as top contributions.
- [ ] Given the same opportunity with GC tier `blocked`, then score = 0 and the reason "GC is on the do-not-bid list" is present.
- [ ] Given a roofing ITB (`trade_relevance=none`), then score ≤ 5.
- [ ] Given a mandatory pre-bid dated yesterday, then score ≤ 10 with the pre-bid reason.
- [ ] Given no size information, then size factor = 0.5 and `missing_inputs` contains "size".
- [ ] Given three `bidding` opportunities already due the same week, then the timing factor is multiplied by 0.6 and the explanation says so.
- [ ] Given the chief estimator changes higher_education from 1.0 to 0.8 and activates v2, then all opportunities are rescored, `profile_version=2` is stored on each score, and the digest lists opportunities whose band changed.
- [ ] Given a profile edit where weights sum to 1.05, then saving is rejected with a validation message.
- [ ] Given identical inputs scored twice, then scores and explanations are byte-identical.

## Edge Cases and Required Tests

| Case | Expected | Test |
|---|---|---|
| Electrical value stated *and* project value stated with an inconsistent ratio (e.g., $5M electrical on $6M project) | Use stated electrical; add reason "electrical share unusually high, verify" | `test_inconsistent_size_signals` |
| Size exactly at band boundaries ($250K, $4M, $8M, $15M) | Factor values 1.0, 1.0, 0.2, 0 respectively | `test_size_boundaries` |
| Size range ($1.2–1.5M) | Midpoint used; explanation shows range | `test_size_range_midpoint` |
| Distance unknown because geocode failed | Factor 0.6; `missing_inputs` includes "location" | `test_distance_unknown` |
| Location in WV, 70 miles | Factor by curve (~0.45), no state penalty | `test_wv_distance` |
| Due date unknown | Timing 0.5; digest shows "due date unknown" | `test_timing_unknown` |
| Due date in 1 day, score otherwise high | Timing 0.2; band may drop to Consider; reason "due in 1 day" | `test_due_tomorrow` |
| Bid due passed but opportunity is `bidding` and marked `submitted` | Not rescored downward; scores freeze at `submitted` | `test_freeze_after_submit` |
| GC tier unknown but domain matches a known GC after the fact | Rescored when GC resolution changes | `test_rescore_on_gc_resolution` |
| GC has 4 bids, 3 wins (below the 5-bid minimum) | No hit-rate adjustment | `test_hit_rate_min_sample` |
| Both `union_required` and `open_shop_indicated` flags (contradictory extraction) | Cap not applied; reason "conflicting labor flags, verify" | `test_conflicting_labor_flags` |
| Boosts push total above 100 | Clamped to 100 | `test_clamp_100` |
| All factors at or below neutral (unknown size, unset GC tier, unknown distance) | No factor is offered as a positive reason; the positive and negative lists never name the same factor | `test_explanation_lists_are_disjoint` |
| Profile with a type missing from the table | Falls back to `other` weight; validation warns | `test_profile_missing_type` |
| Rescoring at midnight moves days-to-due from 7 to 6 | Timing factor changes 1.0 → 0.6 only at the boundary; digest does not report this as a "change" | `test_timing_boundary_not_a_change` |
| Nightly rescoring of 5,000 opportunities | Completes in < 60 s | `test_rescore_performance` |
| Profile version deleted while opportunities reference it | Deletion blocked; versions are immutable | `test_profile_immutable` |

## Technical Notes

- Pure function: `score(opportunity_snapshot, gc_snapshot, calendar_snapshot, profile) -> ScoreResult`. No I/O inside; snapshots assembled by the caller so tests are trivial and results are reproducible.
- Profile JSON schema is published and validated; the admin UI is a thin form over it.
- Calibration harness: `make calibrate` scores the labeled corpus, prints precision/recall per band, and a confusion table by project type and GC tier.
