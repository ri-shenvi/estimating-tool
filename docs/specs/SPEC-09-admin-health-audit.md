# SPEC-09: Administration, Health and Audit

**Status:** Draft · **Priority:** P0 · **Depends on:** all

## Problem

The tool runs unattended every morning for a business whose IT support is part-time. It must be
obvious when it is broken, safe by default, and configurable by non-engineers for everything the
business changes often.

## Goals

- Any failure that would cause a missed or wrong digest is visible within 60 minutes and named in the next digest.
- Setup of a new mailbox source or user takes under 15 minutes by following the runbook.
- Every state change of business significance is auditable.

## Functional Requirements

### F1. Authentication and roles

- Sign-in with Microsoft (Entra ID) for Ferry's tenant; local password accounts allowed for break-glass admin only.
- Roles: `admin`, `chief`, `estimator`, `readonly`. Role matrix:

| Capability | admin | chief | estimator | readonly |
|---|---|---|---|---|
| View opportunities/digests | ✓ | ✓ | ✓ | ✓ |
| Decide/assign/outcome | ✓ | ✓ | ✓ (own or unassigned) | |
| Edit GC tiers, profile | ✓ | ✓ | | |
| Sources, users, secrets | ✓ | | | |
| Audit log | ✓ | ✓ | | |

### F2. Sources page

Add/edit Graph or IMAP source; test connection; folder picker; backfill window; pause/resume; per-source
poll history and error detail; delta-token reset button.

### F3. Health page and checks

| Check | Degraded | Down |
|---|---|---|
| Source poll | last success > 30 min | > 60 min |
| Extraction | error rate > 10% over 1 h | > 50% over 1 h or API unreachable 30 min |
| Queue | oldest pending job > 15 min | > 60 min |
| Digest | sent > 10 min late | not sent by +60 min |
| Database | disk > 80% | unreachable |
| LLM spend | > 2× 7-day average | > configured daily cap (processing pauses, admin alerted) |

Alerts: email to admin list, deduplicated (one on state change, one daily reminder while unresolved).
`/healthz` (liveness) and `/readyz` (readiness: DB + queue) endpoints for the host.

### F4. Audit log

Immutable append-only table with: actor, role, action, entity, before/after JSON, channel, IP, UA,
timestamp. Covers decisions, outcomes, field locks, merges/splits, GC tier changes, profile
activations, source changes, user/role changes, secret rotations, manual re-extractions. Searchable,
exportable to CSV. Retained indefinitely.

### F5. Data retention and privacy

- Raw messages and attachments retained 3 years by default (configurable); opportunities and audit forever.
- Attachments flagged `large_document` retained 90 days unless the opportunity is `bidding`/`submitted`/`won`.
- Secrets (Graph client secret, IMAP password, HMAC key, Anthropic key) stored encrypted at rest with a KMS or env-injected key; never displayed after entry; rotation from the UI.
- Export: full JSON export of opportunities, GCs, decisions for backup or migration.

### F6. Evaluation harness (engineering-facing, exposed as admin report)

- `Eval` page shows the latest run of extraction and scoring evals against the labeled corpus:
  per-field accuracy, classification confusion matrix, band precision/recall, and a diff versus the
  previous prompt/profile version.

### F7. Runbooks

`docs/runbooks/`: connect M365 mailbox (app registration, application access policy, consent),
IMAP fallback, first backfill, rotating secrets, restoring from backup, handling a stuck queue,
changing digest time, onboarding an estimator, offboarding.

## Acceptance Criteria

- [ ] Given an estimator role, when they open the profile editor, then they receive 403 and a link to ask the chief.
- [ ] Given the Graph client secret expires, then within 60 minutes the source is `down`, one alert email is sent, and the next digest names the outage.
- [ ] Given the LLM daily cap is $25 and spend reaches it, then extraction pauses, the queue holds, an alert fires, and the digest states extraction is paused with the count of waiting messages.
- [ ] Given a tier change, a decision, and a profile activation, when the audit log is filtered by actor, then all three appear with before/after.
- [ ] Given `/readyz` while the database is unreachable, then the response is 503.
- [ ] Given a full export is requested, then a JSON archive with all opportunities, GCs, decisions, and profile versions downloads and can be re-imported into an empty instance to produce identical digests for a past date.

## Edge Cases and Required Tests

| Case | Expected | Test |
|---|---|---|
| Alert email itself fails (SMTP down) | Logged; health page shows; `/healthz` stays 200, `/readyz` 200 (alerting is not a readiness dependency) | `test_alert_failure_isolated` |
| Clock skew between app and mail server | Poll staleness computed from app clock only | `test_staleness_app_clock` |
| Two admins edit the same source simultaneously | Optimistic concurrency; second save sees conflict | `test_source_edit_conflict` |
| Audit table write fails | The business action is rolled back (same transaction) | `test_audit_atomic` |
| Break-glass admin login attempted 10 times wrong | Locked 15 minutes; alert | `test_breakglass_lockout` |
| Retention job deletes a raw message referenced by a `won` opportunity | Not deleted; retention skips referenced items in retained statuses | `test_retention_respects_status` |
| Secret rotation while a poll is in flight | In-flight poll completes or fails cleanly; next poll uses new secret | `test_rotate_during_poll` |
| Export of 50,000 messages | Streams; completes < 5 min; memory bounded | `test_export_streaming` |
