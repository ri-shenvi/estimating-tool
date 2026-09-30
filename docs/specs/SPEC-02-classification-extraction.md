# SPEC-02: Message Classification and Opportunity Extraction

**Status:** Implemented · **Priority:** P0 · **Depends on:** SPEC-01 · **Feeds:** SPEC-03, SPEC-04, SPEC-05

## Problem

A raw message is unstructured: a platform notification, a GC's two-line email with a PDF letter, or
a public legal advertisement. Downstream scoring needs a normalized record with typed fields, and
estimators need to know which of those fields are certain and which are guesses.

## Goals

- Classify every message by kind with ≥ 97% accuracy on the labeled corpus for the ITB / not-ITB boundary.
- Extract the opportunity schema with per-field confidence and a verbatim source excerpt.
- Due date accuracy ≥ 97% (exact date), GC name ≥ 98%, project type ≥ 90%, size band ≥ 80%.
- Deterministic and replayable: the same message and prompt version produce the same record; every record stores the prompt version and model.

## Non-Goals

- Reading full drawing sets (large_document attachments are excluded from the prompt).
- Producing the fit score (SPEC-04) or the merge decision (SPEC-03). This spec only produces facts about *this message*.

## Functional Requirements

### F1. Classification

Every `raw_message` receives exactly one `kind`:

| kind | Definition |
|---|---|
| `itb` | A new invitation to bid or request for proposal for construction work that includes or could include electrical scope |
| `rfb` | A request for budget / conceptual pricing / ROM, not a firm bid |
| `addendum` | Issues or forwards an addendum, bulletin, or clarification to an existing solicitation |
| `date_change` | Changes bid due, pre-bid, or RFI dates for an existing solicitation |
| `reminder` | Reminds of an upcoming due date or requests intent to bid; no new information |
| `prebid_notice` | Announces or reminds of a pre-bid meeting or site walk |
| `rfi_response` | Answers to bidder questions |
| `award` | Award, intent to award, regret, or bid results |
| `platform_noise` | Platform digests, "you have unread messages", account notices |
| `not_bid` | Anything else: vendor marketing, invoices, internal mail, newsletters |

Rules:
- Classification uses subject, sender, body, attachment filenames and the first 3,000 characters of attachment text.
- A message that both invites and includes Addendum 1 is `itb` (new information dominates).
- A message that is `itb` but for a trade with no plausible electrical scope (e.g., "Landscaping bid package") is `itb` with `trade_relevance=none`; SPEC-04 will score it near zero. It is not `not_bid`, so the estimator can see it was correctly received and excluded.
- Confidence < 0.6 on kind routes the message to a "Needs review" list on the review page and it is still processed as its best-guess kind.

### F2. Extraction schema

Produced for kinds `itb`, `rfb`, `addendum`, `date_change`, `reminder`, `prebid_notice`,
`rfi_response`, `award`. Each field carries `value`, `confidence` (0–1), and `source` (verbatim
excerpt ≤ 200 chars, plus location: `subject`, `body`, or `attachment:<name>:p<page>`).

```
ExtractedOpportunity
  project_name            str
  project_number          str | null          # GC or owner number
  gc_name                 str                 # inviting company (GC/CM/owner)
  gc_contacts[]           {name, email, phone, role}
  owner_name              str | null
  architect_engineer      str | null
  location                {raw, street, city, state, postal_code}
  project_type            enum(ProjectType)   # see taxonomy
  project_subtype         str | null          # free text: "dialysis clinic", "K-12 addition"
  new_or_renovation       enum(new, renovation, addition, tenant_fit_out, unknown)
  bid_type                enum(hard_bid, budget, gmp, design_build, design_assist, negotiated, unknown)
  sector                  enum(private, public, institutional_private, federal, unknown)
  delivery_channel        enum(buildingconnected, procore, isqft, planhub, smartbid, pantera, dodge, email, public_notice, other)
  bid_due                 {datetime, timezone, time_known: bool}
  prebid                  {datetime, location, mandatory: bool | null}
  rfi_deadline            datetime | null
  intent_due              datetime | null
  anticipated_start       date | null
  duration_months         number | null
  size_signals            {stated_project_value, stated_electrical_value, square_feet, stories, units_or_beds, description}
  scope_items[]           enum(ScopeItem)     # see taxonomy
  scope_text              str                 # verbatim scope paragraph(s) if present
  exclusions_text         str | null
  flags[]                 enum(prevailing_wage, davis_bacon, pla, union_required, open_shop_indicated, bid_bond, pp_bond, liquidated_damages, mbe_wbe_goals, mandatory_prebid, sealed_bid, plans_not_yet_available, tax_exempt, phased, occupied_facility, night_work, background_checks, other)
  document_links[]        {url, host_class, label}
  addendum_number         int | null          # for addendum kind
  changes_described       str | null          # for addendum/date_change: what changed, verbatim
  trade_relevance         enum(primary, partial, none)
  summary                 str                 # ≤ 60 words, plain English, for the digest
  extraction_meta         {model, prompt_version, input_tokens, output_tokens, latency_ms}
```

**ProjectType taxonomy** (aligned to Ferry's stated markets, with additions for correct exclusion):
`commercial_office`, `healthcare`, `higher_education`, `k12_education`, `high_tech_research_data_center`,
`light_industrial_utility`, `heavy_industrial`, `multifamily_hotel_mixed_use`, `retail_restaurant`,
`religious`, `government_civic`, `parking_transportation`, `residential_single_family`, `site_civil_only`,
`other`, `unknown`.

**ScopeItem taxonomy**: `service_and_distribution`, `branch_power`, `lighting`, `lighting_controls`,
`site_lighting`, `fire_alarm`, `structured_cabling`, `security_access_control`, `av`, `nurse_call`,
`generator_ats`, `ups`, `solar_pv`, `battery_storage`, `ev_charging`, `medium_voltage`,
`temporary_power`, `demolition`, `bim_coordination`, `design_build_engineering`, `controls_bms_interface`,
`lightning_protection`, `other`.

### F3. Extraction method

- One LLM call per message using structured outputs against the schema (Anthropic Messages API, `messages.parse` with the Pydantic model; model and prompt version recorded).
- Input: subject, from, to, sent date (as an anchor for relative dates like "next Thursday"), trimmed body, and text of up to 4 non-large attachments (each capped at 12,000 characters, prioritized: files whose name contains "ITB", "invitation", "scope", "bid form", "addendum" first).
- The prompt instructs: never invent a date; when the year is absent, choose the first occurrence on or after the sent date; when two due dates conflict, return the earliest and add a flag `conflicting_dates` with both in `source`; quote sources verbatim.
- Post-processing (deterministic, no LLM):
  - Dates normalized to `America/New_York` unless another zone is explicit.
  - `bid_due` in the past relative to `sent_at` by more than 2 days → confidence capped at 0.3 and flag `past_due_at_receipt`.
  - Email domains of `gc_contacts` used to canonicalize `gc_name` against the GC directory (SPEC-07) by domain match first, then fuzzy name match ≥ 0.9.
  - Platform sender addresses (`team@buildingconnected.com`, Procore, iSqFt) are never used as the GC; the GC is taken from the display name or body.
  - Location geocoded (cached) to lat/long; failures leave `location.geo=null`.
- Refusal or API failure: retry up to 3 times with backoff; then mark the message `extraction_failed` and include it in the digest's Needs review list with the raw subject and sender, so nothing disappears.

### F4. Re-extraction

- Re-extraction can be triggered per message from the review page (e.g., after a prompt fix); the previous record is retained with `superseded_by`.
- A prompt version bump triggers re-extraction only for messages received in the last 30 days, in the background, lowest priority.

## Acceptance Criteria

- [x] Given a BuildingConnected invitation email from "Jane Doe (PJ Dick)" via `team@buildingconnected.com`, when extracted, then `gc_name="PJ Dick"`, `delivery_channel=buildingconnected`, and the sender address is not used as a contact email.
- [x] Given a GC email whose body says "Bids are due Thursday, October 16th at 2 PM" sent on 2026-09-30, when extracted, then `bid_due.datetime = 2026-10-16T14:00-04:00`, `time_known=true`, and `source` contains the quoted sentence.
- [x] Given a body that says only "bids due 10/16", when extracted, then the date is 2026-10-16, `time_known=false`, confidence ≤ 0.8.
- [x] Given a PDF letter with "MANDATORY pre-bid conference: Tuesday, October 7, 2026 at 10:00 AM, at the site", when extracted, then `prebid.mandatory=true` and `flags` contains `mandatory_prebid`.
- [x] Given the subject "ADDENDUM #2 – Allegheny Health Network – Wexford MOB", when classified, then `kind=addendum`, `addendum_number=2`.
- [x] Given a Procore correspondence "The bid due date has been extended to October 21 at 2:00 PM", when classified, then `kind=date_change`, `bid_due` is 2026-10-21T14:00 and `changes_described` quotes the sentence.
- [x] Given a public advertisement listing "General, HVAC, Plumbing and Electrical prime contracts", when extracted, then `sector=public`, `bid_type=hard_bid`, `flags` contains `prevailing_wage` if stated and `sealed_bid`, and `trade_relevance=primary`.
- [x] Given a "Roofing bid package" ITB, when extracted, then `kind=itb`, `trade_relevance=none`.
- [x] Given a message whose body is empty and whose only content is an attached ITB PDF, when extracted, then all fields come from the attachment and each `source.location` starts with `attachment:`.
- [x] Given a vendor newsletter from a lighting rep, when classified, then `kind=not_bid` and no extraction is run.
- [x] Given an API outage, when extraction fails 3 times, then the message is marked `extraction_failed`, appears in the next digest's Needs review, and is retried automatically when the API recovers.
- [x] Given the same message extracted twice with the same prompt version, then the two records are field-for-field identical except `extraction_meta`.

## Edge Cases and Required Tests

Fixtures live in `tests/fixtures/messages/` as `.eml` files with an adjacent `.expected.json`.

| Case | Expected | Fixture |
|---|---|---|
| Date without year, sent in December, due "January 8" | Next year's January 8 | `date_year_rollover` |
| "Bids due Friday at noon" (relative weekday) | First Friday on/after sent date, 12:00 | `date_relative_weekday` |
| Two due dates: GC's internal "Oct 14 at 10 AM" and owner's "Oct 16 at 2 PM" | `bid_due` = Oct 14 10:00; flag `conflicting_dates` | `date_conflict_gc_owner` |
| Due date given as "10/6" in a message sent 10/8 | flag `past_due_at_receipt`, confidence ≤ 0.3 | `date_already_past` |
| Time given as "2:00 PM CST" for a Pittsburgh project | Stored as Central with zone recorded; digest renders in Eastern | `date_foreign_timezone` |
| "Pre-bid walkthrough optional but strongly encouraged" | `prebid.mandatory=false` | `prebid_optional` |
| Pre-bid date but no bid due date | `bid_due=null`; digest labels "due date unknown" | `no_due_date` |
| Project value stated as "$12M total construction" | `stated_project_value=12,000,000`, `stated_electrical_value=null` | `size_project_value_only` |
| "Electrical budget approximately $850K" | `stated_electrical_value=850,000` | `size_electrical_value` |
| "45,000 SF, 3-story" | `square_feet=45000`, `stories=3` | `size_sf_stories` |
| "$1.2 - 1.5 million" | midpoint 1,350,000 with source showing range | `size_range` |
| Scope paragraph says "Electrical, Fire Alarm, and Tele/Data. Security by Owner." | scope_items = [service_and_distribution?, fire_alarm, structured_cabling]; exclusions_text contains "Security by Owner"; `security_access_control` not in scope_items | `scope_with_exclusion` |
| "Design-build electrical; engineer of record to be provided by EC" | `bid_type=design_build`, scope includes `design_build_engineering` | `design_build` |
| Body in HTML table (Procore) with labels "Bid Due" / "Pre-Bid RFI Deadline" | Labels mapped correctly | `procore_table` |
| Forwarded thread containing the original ITB and three replies | Extraction anchored on the innermost original; reply chatter ignored | `forwarded_thread` |
| GC name only in signature block image (no text) but domain is `@mascaroconstruction.com` | `gc_name="Mascaro Construction"` via domain match, confidence ≥ 0.9 | `gc_from_domain` |
| Two GCs mentioned: CM "Turner" inviting, owner "UPMC" | `gc_name=Turner`, `owner_name=UPMC` | `cm_and_owner` |
| Non-English or garbled OCR text | Extraction runs; low confidence fields; no crash | `ocr_garbled` |
| Attachment text 400,000 characters (spec book) | Truncated to per-attachment cap with head+tail strategy; flag `attachment_truncated` | `huge_spec_book` |
| Message that is both an ITB and asks for a budget "for now" | `kind=rfb` when the body says pricing is budgetary; else `itb` | `itb_vs_rfb` |
| "Please disregard the previous invitation; project cancelled" | `kind=award` with `changes_described="cancelled"` (award kind covers cancellations) | `cancelled` |
| Address is only "downtown Pittsburgh" | `location.raw` kept; geocode to city centroid with `geo_precision=city` | `vague_location` |
| Address is in Morgantown, WV | Geocoded; not rejected as out of state | `wv_location` |
| Numbers with typos "2:00 PPM" | Time parsed as 14:00 with confidence reduced | `typo_time` |
| Model returns a schema-valid record with an empty `summary` | Post-processor synthesizes a summary from fields; flag `summary_synthesized` | `empty_summary` |
| Model refusal (`stop_reason=refusal`) | Treated as failure with retry; logged with category | `refusal_stub` |

## Technical Notes

- Prompt and schema are versioned together (`prompts/extract_v{n}.md`, `schemas/opportunity_v{n}.py`); the eval harness (`make eval-extraction`) runs every fixture and reports per-field accuracy; CI fails if due-date accuracy drops below 97% on the fixture set.
- Structured outputs guarantee schema validity; they do not guarantee truthfulness. The post-processor enforces the date rules above regardless of what the model returned.
- Costs: ~4–8K input tokens per message; at 200 messages/day this is a few dollars a day on Opus 5.5. Not worth batch mode; latency of a few seconds is fine.
