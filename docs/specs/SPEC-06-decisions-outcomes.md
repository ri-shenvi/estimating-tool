# SPEC-06: Decisions, Assignments and Outcomes

**Status:** Draft · **Priority:** P0 (auto-reply P1) · **Depends on:** SPEC-03, SPEC-05 · **Feeds:** SPEC-04, SPEC-07

## Problem

A triage tool that does not capture the decision is a newsletter. Decisions must be recordable
from the digest in one click, attributable, reversible, and they must feed GC statistics and future
scoring.

## Goals

- ≥ 80% of bid/pass decisions recorded through the tool within 30 days of launch.
- A decision takes one click from the digest and ≤ 3 from the review page.
- Every decision and outcome is attributed, timestamped, and undoable.

## Non-Goals

- Workflow beyond decision and outcome (no task lists, no document checklists) in v1.

## Functional Requirements

### F1. Actions

| Action | Effect | Available from |
|---|---|---|
| **Bid** | status → `bidding`; assignee defaults to actor (or prompts chief to assign); adds due/pre-bid to calendar feed | digest, review |
| **Pass** | status → `passed`; optional reason from a fixed list (too big, too small, wrong type, GC, too far, no capacity, open shop, other + note) | digest, review |
| **Assign** | sets assignee; if status `new` → `undecided` | digest (dropdown of estimators), review |
| **Snooze** | status → `snoozed` with `snooze_until` (1, 3, 7 days or a date); auto-returns to `undecided`; broken early if due date is within 2 days of `snooze_until` | digest, review |
| **Needs info** | status stays; adds a note and a reminder in the next digest | review |
| **Undo** | reverts the last decision within 7 days; history preserved | digest confirmation page, review |
| **Mark submitted** | status → `submitted`; captures submitted price, date, alternates note | review |
| **Outcome** | `won` / `lost` / `no_award` / `cancelled`; captures award price if known, competitor if known, notes | review, or from an `award` message (SPEC-03) pending confirmation |
| **Lock field** | freezes a canonical field against automatic updates | review |

### F2. One-click links from email

- Each action link carries a signed token: `opportunity_id`, `action`, `recipient_id`, `issued_at`, `nonce`; HMAC with a server secret; expires 7 days.
- Clicking executes the action **only** for GET-safe actions? No: GET requests from email clients and link scanners (SafeLinks, corporate proxies) pre-fetch URLs. Therefore the link opens a minimal confirmation page that performs the action on a POST (one button, auto-focused). Exception: none. Pre-fetch protection is mandatory.
- The confirmation page shows the item summary, performs the action on submit, then shows "Marked as bidding · Undo" and a link to the review page.
- Tokens are single-use per action; a second click shows the current status instead of erroring.
- Tokens are bound to the recipient; a forwarded digest link executes as the original recipient and is logged as `via_forwarded_link=true`. Chief can disable forwarding execution.

### F3. Review page

- `/opportunities/{id}`: all canonical fields with confidence chips and source excerpts on hover, sources timeline (messages, addenda, changes), score explanation, decision history, notes, actions.
- `/opportunities`: filterable list (status, band, assignee, GC, due range, project type), sortable, with bulk Pass and bulk Assign.
- `/review`: queue of Needs review items (SPEC-02/03) with resolve actions (confirm kind, merge, split, re-extract, dismiss).

### F4. Outcome capture

- Two business days after `bid_due` on a `bidding` job with no `submitted` record, the assignee's digest asks "Did we submit Benedum Hall?" with Yes (captures price) / No (→ passed with reason).
- 30 days after submission with no outcome, the digest asks for an outcome; repeats every 30 days up to 3 times, then stops with status `submitted` and flag `outcome_unknown`.
- Outcomes update GC stats (SPEC-07) immediately.

### F5. Auto-reply to GC (P1, off by default)

- When enabled per GC or globally, **Bid** sends a templated will-bid email and **Pass** sends a templated decline, from the estimator's mailbox via Graph `sendMail` (so it appears in Sent Items) and threads on the original message.
- Not sent for platform-originated invitations (BuildingConnected, Procore) where intent should be set in the platform; instead the confirmation page shows "Set intent in BuildingConnected" with the deep link.
- A 10-minute delay with cancel from the confirmation page; the digest's next edition lists auto-replies sent.

### F6. Audit

Every action writes an immutable `audit_event`: actor, action, opportunity, before/after, channel
(digest link, review page, API, system), IP/user agent, timestamp.

## Acceptance Criteria

- [ ] Given a digest Bid link, when clicked, then a confirmation page loads without login; when its button is submitted, then status is `bidding`, assignee is the recipient, and the page offers Undo.
- [ ] Given a link scanner GETs the Bid link 30 times, then no status change occurs.
- [ ] Given the same Bid link clicked twice, then the second click shows "Already bidding since 7:02 AM" with no new audit event.
- [ ] Given Pass is chosen with reason "too far", then the opportunity's `pass_reason=too_far` is stored and GC stats count an invite without a bid.
- [ ] Given Snooze 7d on a job due in 5 days, then the snooze is set to due date minus 2 days and the confirmation page says so.
- [ ] Given a `bidding` job two business days past due with no submission recorded, then the assignee's digest asks whether it was submitted.
- [ ] Given Outcome `won` with price, then GC stats show one more win and the opportunity is `won`.
- [ ] Given a token older than 7 days, then the link redirects to the login-protected review page.
- [ ] Given Undo within 7 days of Pass, then status returns to the previous value and both events remain in history.

## Edge Cases and Required Tests

| Case | Expected | Test |
|---|---|---|
| Two estimators click Bid on the same unassigned item within seconds | First wins; second sees "Assigned to Dana at 7:01, take over?" | `test_concurrent_bid_clicks` |
| Chief clicks Pass on an item an estimator marked Bid an hour earlier | Allowed; audit shows both; estimator notified in next digest | `test_override_by_chief` |
| Token tampered (one byte changed) | 400 with no information leakage; logged | `test_token_tamper` |
| Token for an archived opportunity | Confirmation page says archived; offers reactivate | `test_token_archived` |
| Server secret rotated | Old tokens invalid; digest re-sent links on request | `test_secret_rotation` |
| Pass with reason "other" and empty note | Accepted; note optional | `test_pass_other_no_note` |
| Mark submitted with price 0 or blank | Price optional; warning shown | `test_submit_no_price` |
| Outcome recorded from an award email, then estimator says it was wrong | Outcome reverted; source message marked `misclassified`; feeds eval set | `test_outcome_correction` |
| Auto-reply enabled but GC contact email missing | No email sent; confirmation page notes it | `test_autoreply_no_contact` |
| Auto-reply cancel clicked at minute 9 | Not sent | `test_autoreply_cancel` |
| Auto-reply on platform-originated invite | Not sent; deep link shown | `test_autoreply_platform_skip` |
| Bulk Pass on 25 items | One audit event per item; single confirmation | `test_bulk_pass` |
| Undo after 8 days | Refused with explanation; manual status change still possible from review page | `test_undo_window` |
| Estimator account deactivated with 6 assigned bids | Bids remain assigned; digest to chief lists them as "assignee inactive" | `test_deactivated_assignee` |
| Snooze until a weekend | Returns on that day; appears in Monday's digest | `test_snooze_weekend` |
| Status transition not allowed (won → bidding) | Rejected with allowed transitions listed | `test_invalid_transition` |

## Technical Notes

- Tokens: `base64url(payload) + "." + base64url(hmac_sha256(secret, payload))`; payload includes `v=1` for future rotation. Single-use enforced by `(opportunity_id, action, nonce)` uniqueness in `action_tokens_used`.
- Review page is server-rendered (see ADR-006) with progressive enhancement; every action works without JavaScript.
