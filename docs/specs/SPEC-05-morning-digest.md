# SPEC-05: Morning Digest

**Status:** Draft · **Priority:** P0 · **Depends on:** SPEC-03, SPEC-04, SPEC-06 · **Feeds:** SPEC-06

## Problem

The estimators' morning starts in a noisy inbox. The product's primary surface is a single email
that answers: what is new, what is due, what changed, what needs my decision, and is the system
healthy. It must be readable in five minutes on a phone.

## Goals

- Delivered on every business day at the configured time (default 06:30 America/New_York) to each recipient, personalized by assignment.
- Everything in it is actionable in one click without logging in.
- Nothing important is omitted: every new ITB, every change on a bidding job, every open decision.
- Readable: top section fits in the first screen of a phone email client.

## Non-Goals

- Being a dashboard. The review page exists for depth; the digest is a briefing.
- Real-time alerts (except the P1 "due today" nudge).

## Functional Requirements

### F1. Schedule

- One digest per recipient per business day, at `digest_time` in `digest_timezone`. Weekends and configured holidays are skipped unless `send_on_weekends=true`; items accrue.
- Generation starts 10 minutes before send time after a final rescoring pass; the digest is rendered from a **snapshot** so that everyone receives consistent numbers.
- If generation fails, retry every 5 minutes for 60 minutes; after that, send a minimal fallback digest (counts and a link) plus an admin alert. Never send nothing silently.
- Manual "send me the digest now" button on the review page (personal, not to all).

### F2. Recipients and personalization

- Recipient list configurable with roles: `chief` (sees everything), `estimator` (sees everything, with their assignments first and highlighted), `readonly` (president: no action links).
- A recipient can set `min_band` to hide Likely pass / Pass items behind a count.

### F3. Sections, in order

1. **Header**: date, one-line counts ("4 new · 3 due this week · 2 changed · 1 needs your decision · mailbox OK").
2. **Needs your decision** (assigned to recipient, or unassigned for chief): opportunities in `new`/`undecided` with due date within 10 days, or older than 3 days without a decision. Sorted by due date.
3. **New since last digest**: opportunities first seen since the previous digest for this recipient, grouped by band (Bid expanded, Consider collapsed, Likely pass one line, Pass as count). Within band sorted by score desc, then due date asc.
4. **Due this week**: all `bidding` and `undecided` opportunities with `bid_due` or `prebid` in the next 7 days, as a compact table: date/time, project, GC, status, assignee, addenda count, "docs?" indicator.
5. **Changed**: opportunities with `changed_since_digest` and status in (`bidding`, `undecided`, `new`, `snoozed`), each with its `change_summary`; plus one-liners for `passed` jobs that changed materially.
6. **Needs review**: extraction failures, low-confidence classifications, possible duplicates, orphan updates, addendum gaps. Each with a link to the review page.
7. **System health**: per-source status and last successful poll; last extraction success; number of messages processed. Only expanded when something is degraded.
8. **Footer**: link to review page, calendar feed link, profile version, unsubscribe-from-weekend or preference link.

### F4. Item card (New and Needs decision sections)

```
[82 · BID]  Pitt — Benedum Hall Lab Renovation (Higher Ed · Renovation)
GC: Mascaro (Tier A) · Oakland, 6 mi · Est. ~$1.6M electrical (from $12M × 13%)
Due: Thu Oct 16, 2:00 PM (12 days) · Pre-bid: Tue Oct 7, 10 AM (mandatory) · RFIs due Oct 9
Scope: Lighting & controls, branch power, lab equipment connections, fire alarm modifications;
       tele/data by owner. Addenda: none yet. Docs: BuildingConnected.
Why: Higher-ed (+), Tier A GC (+), size in sweet spot (+), 2 other bids due that week (−)
[Bid] [Pass] [Assign ▾] [Snooze 3d] [Open]
```

Rules:
- Dates render in the recipient's timezone with weekday; "today"/"tomorrow" wording when applicable; unknown dates render as "Due date unknown" in bold.
- Scope line is the extraction `summary`, trimmed to 40 words, followed by exclusions if any.
- "Why" line shows the top 3 positive and top 1 negative contributions from SPEC-04.
- Flags render as chips: Prevailing wage, Bond required, Mandatory pre-bid, Open shop?, Public.
- Action links are signed, single-use-per-action tokens (SPEC-06) that work without login and expire in 7 days; after expiry they redirect to the login-protected review page.
- Each card links to the source email in the mailbox when a web link is available (Outlook deep link).

### F5. Empty states

- No new items: section reads "Nothing new since yesterday." The digest still sends if any other section has content.
- Nothing at all and system healthy: send a two-line digest ("Quiet morning. 6 bids in progress; next due Fri Oct 10.") so absence of mail is distinguishable from a broken system.

### F6. Format and delivery

- HTML with plain-text alternative; renders in Outlook desktop (tables, no flexbox), Outlook mobile, iOS Mail, Gmail.
- Subject: `Bids · Tue Sep 30 · 4 new (2 to bid) · 3 due this week`.
- Sent via the configured SMTP or Graph `sendMail`; From address configurable; replies go to the chief estimator, not a no-reply.
- Every digest is stored (HTML, snapshot JSON, recipients, sent_at, message id) and viewable at `/digests/{date}`.
- Delivery failures per recipient are logged and retried 3 times; persistent failure alerts the admin.

### F7. P1 additions

- 08:00 "Due today" nudge for `bidding` jobs due today, only to the assignee.
- Teams channel post with the header counts and Bid-band cards.

## Acceptance Criteria

- [ ] Given digest time 06:30 ET and three recipients, when a business day arrives, then each recipient receives one email between 06:30 and 06:35 with identical counts and their own assignments first.
- [ ] Given a new opportunity scored 82 with a mandatory pre-bid, when rendered, then the card shows band BID, the pre-bid date with "(mandatory)", the "Why" line with three positives and one negative, and five action links.
- [ ] Given an opportunity whose due date moved yesterday, when rendered, then it appears in Changed with "Due date moved Oct 16 → Oct 21 (Addendum 2)" and not in New.
- [ ] Given an opportunity assigned to Dana, when Sam's digest renders, then it appears in Due this week with assignee "Dana" and not in Sam's Needs your decision.
- [ ] Given no new mail and a healthy system, when the digest renders, then the two-line quiet-morning digest is sent.
- [ ] Given the Graph source has been down for 2 hours, when the digest renders, then System health is expanded at the top of the body with the outage time, and the subject is prefixed "⚠︎".
- [ ] Given a recipient with `min_band=consider`, when rendered, then Likely pass and Pass items appear only as a count with a link.
- [ ] Given digest generation throws, when 60 minutes of retries fail, then a fallback digest with counts and a link is sent and an admin alert fires.
- [ ] Given a digest was sent, when `/digests/2026-09-30` is opened, then the exact HTML that was sent is displayed.
- [ ] Given a public holiday configured, when that day arrives, then no digest is sent and the next business day's digest includes items from both days in New.

## Open Question: the Consider cap can omit a due-date-unknown item

**Raised 2026-09-30 from a fixture-corpus run. Needs a product decision before SPEC-05 is
verified; the implementation currently matches this spec, so nothing is broken as written.**

Two rules in this spec compose into a behaviour the PRD rules out:

* F3 / edge cases: Consider is capped at 10 with "+N more".
* Edge cases: an item whose due date is unknown is "sorted last within band".

An item with no due date therefore sorts to the bottom of its band and is the *first* one the cap
removes. The PRD's edge story says the opposite: "when the system cannot find a due date, I want
the item flagged 'due date unknown' rather than **omitted** or guessed."

Observed: in a 31-opportunity run, 5 Consider items fell past the cap and one of them —
`Hazelwood Green Parcel C Parking Structure` — was the only opportunity with no due date. It
survived the digest only because it was separately an orphan stub, which put it in the chief-only
**Needs review** section. An estimator would not have seen it at all, and a due-date-unknown item
that was not also an orphan would have appeared nowhere in the digest body.

The "+N more" count is honest (verified: 5 hidden, 5 reported), so nothing is hidden silently at
the aggregate level. The question is whether an unknown deadline should be treated as *low
priority* (sort last, as specified today) or as *higher risk* (surface it, because an unknown
deadline is the one most likely to be missed).

Three ways to resolve, for whoever owns this spec:

1. **Exempt unknown-due items from the cap** — they are rare by construction, so the cap still does
   its job on volume.
2. **Sort them first within band rather than last**, on the reasoning that a missing deadline is a
   risk signal, not a deprioritizer.
3. **Keep the current behaviour** and amend the PRD story, on the reasoning that "+N more" plus the
   Needs review section is sufficient coverage.

Whichever is chosen, add the matching row to the table below with a named test; `test_unknown_due_rendering`
covers the rendering only, not whether the item reaches the page.

## Edge Cases and Required Tests

| Case | Expected | Test |
|---|---|---|
| 40 new items in one day | Bid band all shown; Consider capped at 10 with "+N more"; rest as counts | `test_large_volume_truncation` |
| Opportunity is both new and needs decision (due in 4 days) | Appears once, in Needs your decision, with a "new" chip | `test_no_duplicate_across_sections` |
| Item's due date is unknown | Sorted last within band; bold "Due date unknown" | `test_unknown_due_rendering` |
| Due date passed overnight, status still `undecided` | Appears in Needs decision as "Due date passed, mark passed or update" | `test_overdue_undecided` |
| Pre-bid is this morning at 10 AM | Header line "Pre-bid TODAY 10:00 AM: ..." above sections | `test_prebid_today_banner` |
| DST transition day | Sent at 06:30 local; dates render correctly | `test_dst_send_time` |
| Recipient timezone differs (president traveling) | Recipient timezone setting honored; default ET | `test_recipient_timezone` |
| HTML email sanitization: project name contains `<script>` from extraction | Escaped in output | `test_html_escaping` |
| Very long project name (200 chars) | Truncated to 90 with ellipsis; full name in title attribute and on review page | `test_long_names` |
| Action token clicked after 7 days | Redirect to login-protected review page for that opportunity | `test_expired_token_redirect` |
| Two digests generated the same day (manual + scheduled) | Scheduled still sends; manual marked `manual=true`; "new since last digest" is computed from the scheduled watermark only | `test_manual_does_not_move_watermark` |
| Plain-text rendering | All sections and links present; no HTML tags | `test_plaintext_alternative` |
| Outlook rendering | HTML validated against a table-only allowlist (no flex/grid, inline styles only) | `test_outlook_safe_markup` |
| Recipient removed at 06:29 | Not sent to them | `test_recipient_snapshot_timing` |
| Snapshot taken, then an opportunity changes at 06:31 | Digest reflects snapshot; change shows tomorrow | `test_snapshot_consistency` |

## Technical Notes

- Rendering via Jinja2 templates with a fixture-driven golden test (`tests/golden/digest_*.html`); changes to templates must update goldens deliberately.
- Snapshot JSON is the API contract; the Teams and plain-text renderers consume the same snapshot.
