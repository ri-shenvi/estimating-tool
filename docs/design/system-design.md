# System Design: bidtriage

**Date:** 2026-09-29 · **Companion:** ADRs in `docs/adr/`, specs in `docs/specs/`.

## 1. Requirements summary

**Functional.** Ingest mail (SPEC-01) → classify and extract (SPEC-02) → resolve into
opportunities with change tracking (SPEC-03) → score (SPEC-04) → deliver a morning digest
(SPEC-05) → capture decisions and outcomes (SPEC-06) → maintain GC directory and profile
(SPEC-07) → calendar feed (SPEC-08) → admin, health, audit (SPEC-09).

**Non-functional.**

| Dimension | Requirement |
|---|---|
| Volume | ≤ 500 messages/day, ≤ 100 opportunities/week, ≤ 10 users |
| Ingestion latency | ≤ 15 min from arrival to stored record; ≤ 30 min to scored opportunity |
| Digest | Sent within 5 minutes of the configured time every business day; never silently skipped |
| Availability | Business hours matter; overnight outages are acceptable if the 06:30 digest still goes out (generation begins 06:20 with retries) |
| Correctness | Due dates: ≥ 97% exact; no silent date overwrite of user-locked fields |
| Determinism | Scoring and matching are pure functions of stored inputs and versioned config |
| Security | Read-only mailbox access; secrets encrypted; signed action links; SSO; audit log |
| Cost | < $100/month infra + < $300/month LLM at expected volume |
| Operability | One image, one database, one bucket; health endpoints; runbooks |

## 2. High-level architecture

```
                 ┌──────────────────────────────────────────────────────────┐
                 │                    Microsoft 365 / IMAP                  │
                 │   estimating@  ·  personal inboxes  ·  forward-to alias  │
                 └───────────────▲───────────────────────────┬──────────────┘
                     Mail.Send   │ (digest, alerts)          │ Mail.Read (delta poll, 5 min)
                                 │                           ▼
┌────────────────────────────────┴───────────────────────────────────────────────────────┐
│                                   bidtriage (one image)                                │
│                                                                                        │
│  worker process                                   api process                          │
│  ┌──────────────────────────────────────┐        ┌──────────────────────────────────┐ │
│  │ scheduler (cron → jobs)              │        │ FastAPI                          │ │
│  │ job runner (SKIP LOCKED loop)        │        │  /            review pages (HTML)│ │
│  │  ├ poll_source(source_id)            │        │  /a/{token}   action confirm     │ │
│  │  ├ extract_message(message_id) ──► Claude API │  /api/*       JSON               │ │
│  │  ├ resolve_message(message_id)       │        │  /calendar/*  ICS                │ │
│  │  ├ score_opportunity(opp_id)         │        │  /admin/*     sources, users,    │ │
│  │  ├ rescore_all(profile_version)      │        │               profile, health    │ │
│  │  ├ build_digest(date) / send_digest  │        │  /healthz /readyz                │ │
│  │  ├ recompute_gc_stats                │        └──────────────┬───────────────────┘ │
│  │  └ retention, health checks          │                       │                     │
│  └───────────────────┬──────────────────┘                       │                     │
│                      │                                          │                     │
│           ┌──────────▼──────────────────────────────────────────▼───────────┐         │
│           │  domain modules: ingestion · extraction · resolution · scoring  │         │
│           │  gcs · digest · decisions · calendar  (pure where possible)     │         │
│           └──────────────────────────┬──────────────────────────────────────┘         │
└──────────────────────────────────────┼────────────────────────────────────────────────┘
                                       │
                 ┌─────────────────────▼──────────────────┐   ┌──────────────────────┐
                 │ PostgreSQL 16 (+pg_trgm)               │   │ Object storage (S3)  │
                 │ messages, attachments, opportunities,  │   │ attachment blobs by  │
                 │ gcs, scores, profiles, digests, jobs,  │   │ sha256               │
                 │ decisions, audit, users, sources       │   │                      │
                 └────────────────────────────────────────┘   └──────────────────────┘
```

External: Anthropic API (extraction), geocoder (Nominatim self-hosted or a commercial API with
caching), Entra ID (SSO), SMTP or Graph (send).

## 3. Data flow

```
poll_source ─► raw_message(+attachments) ─► [dedupe at ingest]
   └─► enqueue extract_message
extract_message ─► prefilter (deny/allow lists) ─► ClaudeExtractor.parse ─► postprocess
   └─► extraction (versioned) ─► enqueue resolve_message
resolve_message ─► candidates(SQL) ─► evidence score ─► attach/merge/create
   └─► field_history, change_summary ─► enqueue score_opportunity (+ gc_stats if new GC)
score_opportunity ─► snapshot(opportunity, gc, calendar) ─► score(profile) ─► score row
06:20 build_digest ─► rescore_all(timing) ─► snapshot per recipient ─► render ─► store
06:30 send_digest ─► per recipient ─► delivery log
click /a/{token} ─► verify ─► confirm page ─► POST ─► decision ─► audit ─► rescore, gc_stats, calendar
```

Every arrow is a job with an idempotency key (`kind:entity_id:version`) so retries never
duplicate work.

## 4. Data model (core tables)

```sql
sources(id, kind graph|imap|file|manual, name, mailbox, config_json_enc, status,
        last_success_at, delta_state_json, paused, backfill_days, backfill_done,
        down_alert_sent_at)                       -- backfill + once-per-outage alert, SPEC-01 F7/F8
source_polls(id, source_id, mode live|backfill, started_at, finished_at, seen, new, dupes,
             errors_json)

raw_messages(id, internet_message_id, content_hash, from_addr, from_name, to_json, cc_json,
             subject, sent_at, sent_at_confidence, received_at, body_text, body_html,
             body_trimmed, headers_json, in_reply_to, references_json, forwarded_by_user_id,
             forwarded_by_addr, forward_note, forward_chain_json, copies, kind, kind_confidence,
             extraction_status, created_at)
message_sources(message_id, source_id, provider_message_id, recipient_path)   -- fan-out
raw_attachments(id, message_id, parent_id, filename, mime, size, sha256, blob_key, text, pages,
                ocr, large_document, oversize, extraction_error)  -- parent_id: member of a ZIP
message_links(id, message_id, url, host_class, wrapped, label)

extractions(id, message_id, version, model, prompt_version, payload_jsonb, tokens_in, tokens_out,
            latency_ms, superseded_by, created_at)

gcs(id, canonical_name, kind, tier, tier_reason, tier_set_by, tier_set_at, key_account,
    notes, payment_notes, created_from, created_at)
gc_aliases(gc_id, alias)      gc_domains(gc_id, domain)      gc_contacts(id, gc_id, name, email, phone, role, last_seen)
gc_stats(gc_id, window, invites, bids, submitted, won, hit_rate, avg_days_notice, computed_at)

opportunities(id, status, gc_id, canonical_jsonb, normalized_name, fingerprint, geo point,
              first_seen_at, last_activity_at, assignee_user_id, snooze_until, changed_since_digest,
              change_summary, flags text[], locked_fields text[], related_project_ids uuid[],
              archived_at)
opportunity_sources(opportunity_id, message_id, role, attached_at, evidence_jsonb)
addenda(id, opportunity_id, label, number, message_id, received_at, summary)
field_history(id, opportunity_id, field, old_jsonb, new_jsonb, message_id, user_id, applied, changed_at)

scoring_profiles(version, json, author_id, note, active, created_at)
scores(id, opportunity_id, profile_version, score, band, explanation_jsonb, inputs_hash, computed_at)

decisions(id, opportunity_id, action, actor_user_id, channel, reason, note, payload_jsonb,
          undone_by, created_at)
outcomes(id, opportunity_id, result, submitted_at, submitted_price, award_price, competitor, notes, created_at)
action_tokens_used(opportunity_id, action, nonce, used_at, PRIMARY KEY(opportunity_id, action, nonce))

digests(id, date, recipient_user_id, manual, snapshot_jsonb, html, text, sent_at, provider_message_id, status)
digest_watermarks(recipient_user_id, last_scheduled_digest_at)

jobs(id, kind, key UNIQUE, payload_jsonb, status, priority, run_at, attempts, max_attempts,
     leased_until, last_error, created_at, finished_at)
audit_events(id, actor_user_id, role, action, entity_type, entity_id, before_jsonb, after_jsonb,
             channel, ip, user_agent, created_at)   -- append-only (no UPDATE/DELETE grants)
users(id, email, name, role, timezone, active, digest_prefs_jsonb)
calendar_tokens(id, user_id, token_hash, scope, revoked_at)
```

Indexes: `raw_messages(internet_message_id)`, `raw_messages(content_hash, received_at)`,
`opportunities USING gin (normalized_name gin_trgm_ops)`, `opportunities(gc_id, status)`,
`opportunities(status, (canonical_jsonb->>'bid_due'))`, `jobs(status, run_at, priority)`,
`audit_events(entity_type, entity_id, created_at)`.

## 5. Module boundaries and contracts

| Module | Depends on | Exposes |
|---|---|---|
| `core` | — | `Settings`, `Session`, `Clock`, `jobs.enqueue/claim/complete`, ids, crypto |
| `ingestion` | core | `MailSource` protocol; `poll(source) -> PollResult`; `parse_eml(bytes) -> ParsedMessage`; attachment text; link harvest |
| `extraction` | core | `Extractor` protocol (`extract(ParsedMessage) -> ExtractedOpportunity`); `ClaudeExtractor`, `FakeExtractor`; `postprocess()` |
| `gcs` | core | `resolve_gc(name, domains) -> GCMatch`; `recompute_stats(gc_id)` |
| `resolution` | core, gcs | `normalize_name()`, `find_candidates()`, `evidence()`, `apply(message, extraction) -> ResolutionResult` |
| `scoring` | core | `score(OpportunitySnapshot, GCSnapshot, CalendarSnapshot, Profile) -> ScoreResult`; `Profile` schema + defaults |
| `decisions` | core, scoring, gcs | `apply_action()`, `sign_token()/verify_token()`, state machine, outcomes |
| `digest` | core, scoring, decisions | `build_snapshot(date, recipient)`, `render_html/text(snapshot)`, `send()` |
| `calendar` | core | `build_ics(scope) -> bytes` |
| `web` | all | routers, templates |
| `worker` | all | scheduler, handlers |

Import-linter contracts enforce that `scoring` and `resolution` import nothing from `web`,
`digest`, or `worker`, and that `core` imports nothing else.

## 6. Key interfaces

**Job kinds and idempotency keys**

| kind | key | retry |
|---|---|---|
| `poll_source` | `poll:{source_id}:{minute_bucket}` | 3, backoff 30 s |
| `extract_message` | `extract:{message_id}:{prompt_version}` | 3, backoff 60 s; pauses under spend cap |
| `resolve_message` | `resolve:{message_id}:{extraction_id}` | 3 |
| `score_opportunity` | `score:{opp_id}:{inputs_hash}` | 3 |
| `rescore_all` | `rescore:{profile_version}:{date}` | 1 |
| `build_digest` | `digest:build:{date}:{recipient_id}` | 12 × 5 min |
| `send_digest` | `digest:send:{date}:{recipient_id}` | 3 |
| `gc_stats` | `gcstats:{gc_id}:{date}` | 3 |
| `health_check` | `health:{minute_bucket}` | 0 |

**Action token** (SPEC-06): `v1.<b64url(json{o,a,r,t,n})>.<b64url(hmac)>`; verify → confirm page → POST.

**JSON API (subset)**: `GET /api/opportunities?status=&band=&assignee=&due_before=`,
`GET /api/opportunities/{id}`, `POST /api/opportunities/{id}/actions`,
`GET /api/gcs`, `PATCH /api/gcs/{id}`, `GET/POST /api/profiles`, `POST /api/profiles/{v}/activate`,
`GET /api/digests/{date}`, `GET /api/health`.

## 7. Scheduling and time

- All persisted times UTC; business logic uses `America/New_York` via a `Clock` abstraction that
  tests can freeze.
- Scheduler ticks every minute inside the worker; it enqueues jobs with minute-bucket keys, so two
  workers cannot double-schedule.
- Digest pipeline: 06:20 `rescore_all` (timing factors), 06:22 `build_digest` per recipient,
  06:30 `send_digest`. If build is late, send waits for it up to +60 min, then sends the fallback.

## 8. Failure modes and handling

| Failure | Detection | Handling |
|---|---|---|
| Graph token expired / consent revoked | poll error; staleness | source `down` at 60 min; alert; digest banner; runbook |
| Delta token 410 | poll error code | reset delta state; full resync; dedupe protects |
| Anthropic API outage or 429 | extract errors | SDK retries; job backoff; queue holds; digest lists waiting count |
| Refusal (`stop_reason=refusal`) | response check | `fallbacks="default"` server-side; if still refused, mark failed, Needs review |
| Schema-valid but wrong date | eval + confidence rules | post-processor caps confidence; digest shows source excerpt; user lock |
| Spend spike | spend meter | pause extraction at cap; alert |
| Worker crash mid-job | lease expiry | job re-claimed after `leased_until`; handlers idempotent |
| Digest render exception | job failure | retries; fallback digest at +60; alert |
| SMTP failure | send error | 3 retries per recipient; alert; digest stored and viewable |
| DB down | readyz | host restarts; nothing lost (jobs are in DB) |
| Blob store down | attachment write error | the message is not committed and its provider id is never linked, so the next poll re-fetches it; the error shows on the `source_polls` row |
| Geocoder down | timeouts | distance factor uses `unknown`; retry nightly |

## 9. Security

- Mailbox: application permission scoped by access policy; read-only. Send permission on a
  separate identity.
- Web: Entra ID OIDC; session cookies `Secure/HttpOnly/SameSite=Lax`; CSRF tokens on all POSTs;
  action-link confirm pages are POST-only for state change.
- Secrets: env-injected master key; per-secret AES-GCM envelope in `sources.config_json_enc`;
  never rendered after entry.
- Audit: append-only table with revoked UPDATE/DELETE for the app role.
- PII: GC contact names/emails only; no consumer data. Retention per SPEC-09.
- Prompt injection: email content is untrusted. The extractor prompt treats message content as
  data inside delimiters; the model has no tools; outputs are schema-constrained and
  post-processed; nothing from a message is ever executed or used to route mail.

## 10. Scale and cost

At 500 messages/day: ~350 LLM calls (after pre-filter) × ~6K tokens ≈ 2M input tokens/day ≈
$8/day on Opus 5.5 plus output. Postgres < 5 GB/year including extraction payloads. Attachments
~20 GB/year. One 2-vCPU VM runs both processes with headroom. Horizontal scale path: N workers
(SKIP LOCKED handles it), API behind a load balancer; nothing is in-process state except caches.

## 11. Observability

Structured JSON logs with `job_id`, `message_id`, `opportunity_id`; metrics counters (polls,
messages, extractions by status, tokens, jobs by kind/status, digest send latency) exposed at
`/metrics` (Prometheus format) and summarized on the health page; error tracking via Sentry-compatible
DSN (optional).

## 12. Testing strategy

- **Unit**: scoring (boundaries, caps, determinism), resolution (every SPEC-03 case), tokens,
  ICS, date post-processing, name normalization. No I/O.
- **Fixture/eval**: `tests/fixtures/messages/*.eml` + `.expected.json` through `FakeExtractor`
  for pipeline tests and through `ClaudeExtractor` for the eval harness (network, run manually or
  nightly). CI gate on due-date accuracy.
- **Integration**: Postgres via docker service in CI; job runner end-to-end from `.eml` to digest
  snapshot; golden HTML for digest.
- **Contract**: import-linter; OpenAPI schema snapshot.
- **Manual checklist**: digest rendering in Outlook desktop/mobile, iOS Mail, Gmail; ICS in
  Outlook, Google, Apple.

## 13. What to revisit as it grows

- Job module → dedicated library if fan-out grows.
- Platform document retrieval (BuildingConnected/Procore) once logins are available: new
  `DocumentSource` protocol; extraction gains a `documents[]` input.
- Learned weight tuning once ≥ 200 outcomes.
- Multi-tenancy: add `tenant_id` to every table now? No: YAGNI for one customer, but keep all
  config in rows rather than env so a tenant column is an additive migration.
