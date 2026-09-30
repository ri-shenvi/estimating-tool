# PRD: bidtriage — ITB Triage and Morning Digest for Ferry Electric

**Status:** Draft v1 · **Date:** 2026-09-29 · **Owner:** Product · **Companion docs:**
`docs/research/bidding-process.md`, `docs/product/00-brainstorm.md`, feature specs in `docs/specs/`,
architecture in `docs/adr/` and `docs/design/system-design.md`.

---

## Problem Statement

Ferry Electric's estimators receive on the order of forty invitations to bid a week across
BuildingConnected, Procore, iSqFt, PlanHub and direct GC email, each of which spawns reminders,
addenda and date changes that arrive as separate, often duplicated messages. Triage happens by
reading a shared inbox, and estimator hours (the department's binding constraint, at 20 to 60
hours per takeoff) get spent on poor-fit jobs while good-fit jobs and their addenda are found late
or missed. The cost is lower hit rate, wasted takeoffs, and the occasional disqualified bid.

## Goals

| # | Goal | Measure | Target (90 days after launch) |
|---|---|---|---|
| G1 | Every inbound ITB is captured and visible the next morning | Share of ITBs in the mailbox that appear as an opportunity in the digest | ≥ 98% |
| G2 | Estimators decide from the digest, not the inbox | Share of bid/pass decisions recorded via digest actions or the review page | ≥ 80% |
| G3 | Scoring agrees with the chief estimator | Agreement between top-quartile score and estimator "bid" decisions on the labeled corpus | ≥ 80% precision, ≥ 85% recall on "bid" |
| G4 | No missed deadline changes | Due-date moves and addenda surfaced in the next digest for every opportunity marked "bidding" | 100% |
| G5 | Triage time drops | Self-reported weekly hours on ITB triage | From ~10 to ≤ 3 |

## Non-Goals (v1)

- **No takeoff or pricing.** The tool decides *whether* to bid, not *what* to bid.
- **No drawing-set analysis.** ITB emails and attached ITB/scope PDFs are read; full plan sets are not (v2, requires portal logins).
- **No bid submission or GC communication on Ferry's behalf** beyond an optional, explicitly enabled will-bid / decline auto-reply (P1).
- **No general CRM.** GC records exist to score fit and store notes, not to run sales.
- **No mobile app.** The digest is email; the review page is a responsive web page.
- **No multi-tenant SaaS.** One deployment for Ferry. Architecture should not preclude it, but nothing is built for it.

## Users

| Persona | Count | Needs |
|---|---|---|
| **Chief estimator** (Casey) | 1 | Owns scoring weights, GC tiers, size bands. Reads the digest first. Assigns bids. |
| **Estimators** (Dana, Sam) | 2 to 4 | Read the digest, act on assigned items, log outcomes. |
| **President / operations** | 1 | Weekly view of what is being bid and backlog implications. Read-only. |
| **Administrator** | 0.2 | Connects the mailbox, manages users, watches ingestion health. May be an outside IT vendor. |

## User Stories

**Chief estimator**
- As the chief estimator, I want a single morning email listing new invitations ranked by fit so that I can assign takeoffs in five minutes.
- As the chief estimator, I want each item to show *why* it scored as it did so that I can trust or override the number.
- As the chief estimator, I want to change GC tiers, size bands and market weights myself so that scoring keeps up with the business without an engineer.
- As the chief estimator, I want to see every addendum and due-date change on jobs we are bidding so that we never bid the wrong scope or miss a moved deadline.
- As the chief estimator, I want to record won/lost and price after bid day so that GC scores and weights improve over time.

**Estimator**
- As an estimator, I want to mark an item bid / pass / assign / snooze from the digest so that the decision is captured without opening another system.
- As an estimator, I want the scope summary to name the systems involved (lighting, gear, generator, fire alarm, low voltage) so that I know what kind of takeoff it is before I open the documents.
- As an estimator, I want bid due dates and pre-bid meetings on my Outlook calendar so that I do not track them by hand.
- As an estimator, I want to forward any email to the system and have it captured so that ITBs sent to me personally are not lost.

**Edge and error stories**
- As an estimator, when the system cannot find a due date, I want the item flagged "due date unknown" rather than omitted or guessed.
- As the chief estimator, when two invitations for the same project arrive from two channels, I want one opportunity with both sources listed, not two entries.
- As an administrator, when the mailbox connection fails, I want to be told in the digest and by alert so that a silent gap does not form.

## Requirements

### P0 — Must have

| ID | Requirement | Spec |
|---|---|---|
| R1 | Ingest messages from a connected Microsoft 365 or IMAP mailbox and from a forward-to address, including PDF/DOCX attachments; idempotent; never deletes or moves source mail | SPEC-01 |
| R2 | Classify each message as ITB, request for budget, addendum, date change, reminder, pre-bid notice, award/regret, or not bid-related | SPEC-02 |
| R3 | Extract a normalized opportunity record: project, GC, contacts, location, owner, project type, bid type, due date/time, pre-bid (with mandatory flag), RFI deadline, size signals, scope items, risk flags, document links; each field with confidence and source excerpt | SPEC-02 |
| R4 | Resolve messages into opportunities: dedupe across channels; attach addenda, reminders and date changes to the right opportunity; record field changes as a diff | SPEC-03 |
| R5 | Compute an explainable fit score (0–100) from project type, estimated electrical size, GC relationship, distance, timing/capacity, and risk flags, using a versioned scoring profile editable by the chief estimator | SPEC-04 |
| R6 | Send a morning digest at a configured time with sections: New, Due this week, Changed, Needs decision, System health; each item with score, reasons, scope summary, key dates, and action links | SPEC-05 |
| R7 | Capture decisions (bid / pass / assign / snooze / needs info) via signed one-click links and a review page; capture outcomes (submitted, won, lost, no-bid, price) | SPEC-06 |
| R8 | Maintain a GC directory with tier, notes, and computed stats (invites, bids, wins, hit rate) | SPEC-07 |
| R9 | Publish an ICS calendar feed of bid due dates and pre-bid meetings for opportunities marked bidding or undecided | SPEC-08 |
| R10 | Admin: mailbox connection, users, scoring profile editor, health dashboard, audit log of decisions and profile changes | SPEC-07, SPEC-09 |

### P1 — Should have (fast follows)

- Optional will-bid / decline auto-reply to the GC when a decision is recorded (SPEC-06, flagged off by default).
- Teams channel post mirroring the digest.
- Weekly summary for the president: bids submitted, hit rate, backlog of bids due.
- Estimator capacity settings (hours available per week) feeding the timing factor.

### P2 — Future considerations (design for, do not build)

- Plan-set peek: pull E-sheet index, fixture and panel schedules from the drawing set via platform APIs or logins.
- Public-notice ingestion (PennBID, PA eMarketplace, university portals).
- Scoring weight tuning from outcomes (learned weights replacing hand-set ones once ≥ 200 labeled outcomes exist).
- Multi-tenant deployment for other NECA contractors.

## Success Metrics

**Leading (first 2 to 4 weeks).**
- Digest open rate ≥ 90% of business days per estimator.
- Median time from digest send to first decision < 2 hours.
- Extraction field accuracy on the labeled corpus: due date ≥ 97%, GC ≥ 98%, project type ≥ 90%, size band ≥ 80%.
- Duplicate opportunity rate < 3%; false merge rate < 1%.

**Lagging (quarter).**
- Estimator triage hours per week (self-reported) ≤ 3.
- Share of takeoffs started on opportunities scored ≥ 70: ≥ 75%.
- Hit rate on submitted bids: baseline first, then +5 points.
- Zero bids disqualified for a missed addendum or moved date.

## Constraints and Assumptions

- Email platform assumed Microsoft 365 (Graph API). IMAP is the fallback for any other host.
- One deployment, low volume (hundreds of messages a day at most). Simplicity beats scale.
- Source email is never modified. Read-only mailbox permission is the target.
- LLM extraction uses the Anthropic API (Claude Opus 5.5 by default). Costs are bounded: at ~200 messages a day with ~4K tokens each, well under $50 a day; batch processing is not required.
- Scoring must be reproducible: the same inputs and profile version always yield the same score.
- Data is Ferry's business data and includes GC pricing behavior; access is authenticated and the audit log is immutable.

## Open Questions

| Question | Owner | Blocking? |
|---|---|---|
| Shared mailbox vs. personal inboxes; M365 confirmed? | Ferry IT / chief estimator | Yes, for SPEC-01 config |
| Initial GC tier list and size band | Chief estimator | Yes, for calibration |
| Does Ferry bid Separations Act prime work? | Chief estimator | No (decides P2 priority) |
| Digest time and recipients | Chief estimator | No (config) |
| Portal logins available to the tool for document download? | Ferry IT | No (v2) |
| Is an auto-reply to GCs acceptable culturally? | Chief estimator | No (P1 flag) |

## Timeline and Phasing

- **Phase 0 (weeks 1–2): corpus and calibration.** Export 60 to 90 days of mailbox history; chief estimator labels bid/pass and rates fit; freeze fixture set. Build SPEC-01/02 against fixtures.
- **Phase 1 (weeks 3–6): pipeline and digest.** SPEC-03, 04, 05 end-to-end on live mail in shadow mode (digest goes to the chief estimator only). Compare to manual triage daily.
- **Phase 2 (weeks 7–8): decisions and calendar.** SPEC-06, 07, 08, 09. Digest goes to all estimators. Shadow mode off.
- **Phase 3 (weeks 9–12): tune.** Weekly scoring review; adjust profile; P1 items as capacity allows.

Hard dates: none contractual. The 2026 centennial is a soft motivation to have the tool in
daily use before year end.
