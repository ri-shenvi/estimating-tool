# ADR-002: PostgreSQL as system of record and job queue

**Status:** Accepted · **Date:** 2026-09-29 · **Deciders:** Engineering lead

## Context

We need durable storage for messages, opportunities, GCs, scores, digests and audit; a job queue
for polling, extraction, scoring, digest generation; fuzzy text matching for dedupe; and a
scheduler. Ops capacity is minimal.

## Decision

PostgreSQL 16 for everything: relational data, JSONB for extraction payloads and profile versions,
`pg_trgm` for name similarity, and a `jobs` table drained with `SELECT ... FOR UPDATE SKIP LOCKED`
by the worker. Scheduling via the worker's in-process cron (APScheduler) writing jobs, with
idempotency keys so duplicate schedulers cannot double-send a digest. Attachments in
S3-compatible object storage (local disk in dev).

## Options Considered

### Option A: Postgres for data + Postgres-backed jobs (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low: one stateful service |
| Cost | One managed instance |
| Scalability | Thousands of jobs/min, far beyond need |
| Team familiarity | High |

**Pros:** transactional enqueue (write opportunity and its scoring job atomically); no second
system to back up or monitor; SKIP LOCKED is a well-worn pattern.
**Cons:** no built-in dashboard; we write a ~150-line worker loop and a jobs admin page.

### Option B: Postgres + Redis + Celery/RQ
**Pros:** mature tooling, retries, dashboards (Flower). **Cons:** second stateful service, non-transactional enqueue, more failure modes; overkill at this scale.

### Option C: Managed queue (SQS) + Postgres
**Pros:** zero-ops queue. **Cons:** cloud lock-in for a customer whose hosting is undecided; local dev friction.

### Option D: SQLite
**Pros:** simplest. **Cons:** single-writer, weak concurrency between api and worker, no trigram indexes. Rejected.

## Consequences

- Easier: backups, local dev (one docker container), atomic writes.
- Harder: long-running jobs need heartbeat/lease renewal (implemented in the worker).
- Revisit: if job volume or fan-out grows, swap the `jobs` module for a dedicated library (procrastinate) without touching call sites.

## Action Items
1. [x] `jobs` table, enqueue/claim/complete/fail API, lease expiry, exponential backoff.
2. [x] Alembic migrations from day one.
3. [ ] Nightly `pg_dump` to object storage; restore drill in runbook.
