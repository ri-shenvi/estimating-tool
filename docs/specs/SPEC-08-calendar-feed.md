# SPEC-08: Bid Calendar Feed

**Status:** Draft · **Priority:** P0 · **Depends on:** SPEC-03, SPEC-06

## Problem

Due dates and pre-bid meetings live in the digest and on the review page, but estimators plan their
week in Outlook. Re-typing dates is error-prone and the first thing to drift.

## Goals

- Every `bidding` and `undecided` opportunity's bid due and pre-bid appear in subscribers' calendars within one refresh cycle of a change.
- Zero manual entry.

## Non-Goals

- Writing events into individual mailboxes via Graph (P1 option; ICS subscription is the v1 mechanism to avoid write permissions).
- Two-way sync.

## Functional Requirements

### F1. Feeds

- `/calendar/{token}.ics` per user (personal: own assignments plus unassigned `undecided`) and `/calendar/team/{token}.ics` (everything `bidding`/`undecided`).
- Tokens are long random secrets revocable from settings.
- Filters via query: `?status=bidding`, `?assignee=me`, `?band=bid,consider`.

### F2. Events

| Event | When | Title | Duration | Alarms |
|---|---|---|---|---|
| Bid due | `bid_due` with time; if `time_known=false`, all-day event | `BID DUE: {project} ({GC})` | 30 min ending at due time | −1 day, −2 hours |
| Pre-bid | `prebid.datetime` | `PRE-BID{ (MANDATORY)}: {project} ({GC})` with location | 90 min | −1 day |
| RFI deadline | `rfi_deadline` | `RFIs DUE: {project}` | all-day | −1 day |
| Intent due | `intent_due` | `Intent to bid due: {project}` | all-day | none |

Description includes: status, assignee, score and band, scope summary, addenda count, document
link, review page link. `UID` is stable per (opportunity, event type) so updates replace rather
than duplicate. `SEQUENCE` increments on every change. `LAST-MODIFIED` set.

### F3. Lifecycle

- `passed`, `lost`, `cancelled`, `archived` → events removed (feed omits them; `STATUS:CANCELLED` emitted for 7 days so clients that cache can clear them).
- `submitted` → bid due event retitled `SUBMITTED: {project}`; pre-bid events removed.
- Unknown due date → no due event; a note in the digest instead.
- Timezone: `VTIMEZONE` for `America/New_York` embedded; other explicit zones preserved.

### F4. Performance

Feed generated on request with a 5-minute cache; ≤ 2 s for 500 events.

## Acceptance Criteria

- [ ] Given a `bidding` job due Oct 16 2:00 PM with a mandatory pre-bid Oct 7 10:00 AM, when the feed is fetched, then two VEVENTs exist with the specified titles, times in America/New_York, and alarms.
- [ ] Given the due date changes to Oct 21, when refetched, then the same UID appears with the new DTSTART and incremented SEQUENCE.
- [ ] Given the job is marked passed, when refetched, then the events carry `STATUS:CANCELLED` and disappear after 7 days.
- [ ] Given `time_known=false`, then the due event is all-day on the due date.
- [ ] Given the feed is imported into Outlook, Google Calendar, and Apple Calendar, then events render at the correct local time and updates replace originals (manual verification checklist).

## Edge Cases and Required Tests

| Case | Expected | Test |
|---|---|---|
| Due time given in Central for a Pittsburgh job | Event at the Central instant, displayed in the subscriber's zone | `test_foreign_tz_event` |
| Two opportunities due at the same instant | Two distinct UIDs | `test_same_time_distinct_uids` |
| Project name with commas, semicolons, newlines | ICS escaping correct | `test_ics_escaping` |
| Very long description (10 KB) | Line folding at 75 octets | `test_line_folding` |
| Token revoked | 404, not 403 (do not confirm existence) | `test_revoked_token` |
| Feed requested 1,000 times in 5 minutes (aggressive client) | Cache serves; no regeneration storm | `test_feed_cache` |
| Pre-bid in the past but due in the future | Pre-bid event remains (historical), due event present | `test_past_prebid_kept` |
| Opportunity un-archived | Events reappear with SEQUENCE higher than before | `test_reactivated_sequence` |
