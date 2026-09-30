# SPEC-10: Ingestion Hardening

**Status:** Draft · **Priority:** P0 · **Depends on:** SPEC-01 · **Feeds:** SPEC-09

## Problem

SPEC-01 is implemented and its acceptance criteria pass, but a review of the implementation found
defects that the SPEC-01 test suite does not catch. Three of them break SPEC-01's own goals:

1. **Messages are silently and permanently lost.** Both transports advance their cursor past a
   message they failed to fetch. A single 503 from Graph or a `NO` from an IMAP `FETCH` drops that
   ITB forever — the delta token / `last_uid` has already moved on. Demonstrated: a transient
   failure on one message of three leaves it unrecoverable on every later poll. This directly
   contradicts SPEC-01's first goal, "capture 100% of messages".
2. **The worker runs out of memory on ordinary mail.** A source hands `run_poll` the entire batch
   fully parsed, with every attachment's bytes resident. 20 messages carrying a 3 MB PDF each
   already hold 63 MB; the default `ingest_live_batch=200` extrapolates to ~630 MB, and SPEC-01 F5
   explicitly admits attachments up to 200 MB and drawing sets of 50–200 MB. One bid package with
   three drawing sets ends the worker.
3. **Ingestion stops roughly an hour after the worker starts.** `GraphSource._auth` caches the
   bearer token with no expiry, and the worker caches the `GraphSource` instance for its whole
   lifetime. Once the token expires every call 401s, so `last_success_at` stops advancing, and at
   60 minutes the source is reported `down` and an admin alert fires — while nothing is actually
   wrong with the mailbox or the credentials.

Alongside these, deduplication has no constraint enforcing it (two workers can both insert the same
`Message-ID`), the first-connection backfill stalls permanently if one of its batch jobs exhausts
its retries, and the admin page recomputes ingestion metrics by loading every message in the
trailing window (2.24 s and 317 MB at the 30,000-message scale SPEC-01 itself names as an edge
case).

Note the interaction between defect 1 and SPEC-01's health rules: because a poll that reached the
mailbox counts as successful even when individual messages failed, per-message loss shows up only
as a transient `source_polls.errors` entry. Nothing alerts, and nothing retries.

## Goals

- No message is dropped because of a transient fetch failure: a cursor only advances past messages that were durably stored.
- Worker memory stays bounded by a configured limit regardless of batch size or attachment size.
- A long-running worker keeps polling indefinitely without restarts, and credential rotation takes effect without one.
- Duplicate suppression is enforced by the schema, not only by a preceding read.
- Backfill either completes or reports itself stuck; it never stalls silently.
- Operational pages and metrics stay responsive at the volumes SPEC-01 names (30,000 messages/day).

## Non-Goals

- Changing the SPEC-01 data model beyond the columns and constraints named below.
- Streaming attachment bytes straight to blob storage without ever holding one in memory. One attachment at a time is in scope; zero-copy is not.
- Reworking the job queue. Backfill recovery uses the existing `jobs` table.
- Re-alerting on a continuing outage. SPEC-01 F8 says once per outage; that stays.

## Functional Requirements

### F1. Cursors advance only over stored messages

A source's stored cursor must never move past a message that was not committed.

| Transport | Rule |
|---|---|
| Graph | Store the `@odata.deltaLink` only when every message in the page was handed to the caller *and* the caller confirmed it stored them. A page with a failed `_mime` fetch keeps the previous cursor and is retried next poll. |
| IMAP | `last_uid` advances to the highest UID *below the first failure*, never to the maximum successfully fetched UID. A failed UID is re-attempted on the next poll. |

Because the store step happens after the fetch step, the source can no longer decide this alone.
`run_poll` becomes the authority: it reports back which provider ids committed, and the source
computes its new state from that. `PollResult.new_state` is therefore replaced by a
`commit_state(stored_ids) -> dict` callback on the source, or equivalent.

A message that fails repeatedly must not block the mailbox forever. After `INGEST_MAX_FETCH_ATTEMPTS`
(default 5) consecutive failures for the same provider id, it is recorded in a new
`source_skips` table (source id, provider message id, first seen, attempts, last error), the cursor
is allowed past it, and it appears in the digest's Needs review section. Loss becomes visible and
deliberate instead of silent.

### F2. Bounded batch memory

- Sources yield messages **one at a time** (a generator), not a materialised list. `poll` and `backfill` return an iterator of `(provider_message_id, ParsedMessage)` plus a state handle.
- `run_poll` stores and commits each message, then drops its reference before pulling the next.
- A new `INGEST_MAX_BATCH_BYTES` (default 256 MiB) caps the cumulative *attachment* bytes handled in one poll. On reaching it the poll stops early and reports `more_available=True`; the next poll continues from the unadvanced cursor.
- A single message whose attachments exceed the cap is still processed alone, so one big bid package cannot deadlock the mailbox.
- `INGEST_MAX_ATTACHMENTS_PER_MESSAGE` (default 50) caps attachment rows per message; the overflow is counted on the message and flagged for review.

### F3. Graph token lifecycle and credential freshness

- `GraphSource` records the token's expiry from the token response's `expires_in` and refreshes it when fewer than 120 seconds remain.
- A 401 response invalidates the cached token and retries the call once before failing.
- `Context.source_impl` caches an implementation keyed by `(source_id, config fingerprint)`, where the fingerprint is a hash of `config_enc`. Rotating a secret in the database rebuilds the implementation on the next poll, with no worker restart.

### F4. Deduplication enforced by the schema

- Partial unique index on `raw_messages(internet_message_id) WHERE internet_message_id IS NOT NULL`.
- `ingest_parsed` catches the resulting `IntegrityError`, rolls back to a savepoint, re-reads the winning row, and links to it as a duplicate. The read-then-write path stays as the fast path; the constraint is the backstop.
- The content-hash duplicate query is ordered by `received_at ASC` so the *earliest* matching message is always the link target, making the outcome deterministic.
- `raw_messages.copies` is derived from `count(distinct source_id)` over `message_sources` rather than incremented in place, so it cannot drift. Expose it as a read-through property or maintain it in one place with a test that asserts agreement.
- SQLite has no partial unique indexes over expressions in older versions; the index is created with a dialect guard and the behaviour is covered by a Postgres-marked test.

### F5. Backfill never stalls silently

- `schedule_tick` enqueues `backfill_source` for every source where `backfill_done` is false, on the same 5-minute bucket key as `poll_source`. Chaining stays as the fast path, but the scheduler is the safety net, so a batch job that exhausts its retries is picked up on the next tick.
- `Source` gains `backfill_attempts` and `backfill_last_error`. After `INGEST_MAX_BACKFILL_ATTEMPTS` (default 20) failures the source is marked `backfill_stuck` and surfaces on the admin page and in the digest's System health section.
- `_seed_live_cursor` moves **inside** `run_poll`'s error handling, so a failing `seed` finalises the `source_poll` row and sets `status='error'` like any other transport failure.
- A source is seeded only when it implements **both** `seed` and `backfill`. Seeding a source that cannot walk history would skip that history entirely.

### F6. Poll bookkeeping

- Any `source_poll` row with `finished_at IS NULL` and `started_at` older than 30 minutes is closed by the scheduler as `errors=['abandoned: worker died mid-poll']`, so a crashed poll does not linger as the source's apparent last poll forever.
- `source_polls` are pruned after `INGEST_POLL_RETENTION_DAYS` (default 30). At one poll per 5 minutes per source that bounds the table at ~8.6 k rows per source.
- Index on `source_polls(source_id, started_at DESC)`, and every "latest poll" query uses an explicit `LIMIT 1`.

### F7. Trustworthy receipt timestamps

`received_at` currently comes from the message's topmost `Received:` header, which a sender can
forge, and it feeds both the 7-day dedupe window and the lag metric.

- `received_at` is the time the poller committed the message, except where the transport supplies its own authoritative value (Graph `receivedDateTime`, IMAP `INTERNALDATE`), which is preferred.
- A `Received:`-derived timestamp is used only when no transport value exists (`.eml`/`.msg` upload) and is clamped to `[now - backfill_days, now]`.
- Negative lag (`created_at < received_at`) is counted in a `clock_skew` metric rather than silently dropped.

### F8. Metrics and admin page at volume

- `ingestion_metrics` is computed with aggregate SQL — `COUNT`/`SUM` over `source_polls`, a grouped count over `raw_attachments` joined to the window, and lag percentiles via `percentile_cont` on Postgres with a sampled fallback elsewhere. No query loads message bodies.
- Target: under 100 ms at 30,000 messages in the window (currently 2.24 s / 317 MB).
- The admin page reads metrics from a short-lived cache (60 s) so repeated refreshes do not recompute.

### F9. Upload and container limits

- The upload endpoint enforces `INGEST_MAX_UPLOAD_BYTES` (default 64 MiB), streaming to a temporary file and rejecting oversize input with 413 before the whole body is resident.
- `extract_zip` caps the entry **listing** at 1,000 entries (noting the truncation in the stored text), not just the extracted members.
- ZIP members are read through a size-limited reader rather than trusting the central directory's `file_size`, so a header that understates the uncompressed size cannot bypass `ZIP_MAX_TOTAL`.

### F10. Alert delivery is per recipient

`_alert` tracks which recipients were notified and reports success if at least one send succeeded, so
a single bad address no longer causes the entire alert to be re-sent to everyone on the next sweep.

## Acceptance Criteria

- [ ] Given a folder of 3 messages where fetching the first fails transiently, when the poller runs and then runs again after the failure clears, then all 3 messages exist and the cursor never skipped the first.
- [ ] Given the same message failing 5 consecutive polls, when the limit is reached, then a `source_skips` row exists, the cursor advances, and the skip appears in the digest's Needs review section.
- [ ] Given a batch of 20 messages each carrying a 3 MB attachment, when the poller runs, then peak process memory attributable to the batch stays under 64 MB.
- [ ] Given a single message with a 180 MB attachment, when the poller runs, then it is processed alone and the poll reports `more_available=True`.
- [ ] Given a Graph token that expires mid-run, when the next poll happens after expiry, then the token is refreshed transparently, the poll succeeds, `last_success_at` advances, and no down alert is sent.
- [ ] Given a source whose `config_enc` is rewritten with new credentials, when the next poll runs in the same worker process, then the new credentials are used.
- [ ] Given two concurrent transactions that both observe no duplicate for the same `Message-ID`, when both commit, then exactly one `raw_message` exists and the loser is linked as a duplicate with its own `message_sources` row.
- [ ] Given a message reachable from three sources, when all three are polled in any order, then `copies` equals 3 and equals `count(distinct source_id)` over its `message_sources`.
- [ ] Given a `backfill_source` job that fails until it exhausts `max_attempts`, when the scheduler next ticks, then a new backfill job is enqueued and the walk resumes.
- [ ] Given 20 consecutive backfill failures, then the source shows `backfill_stuck` on the admin page and in the digest's System health section.
- [ ] Given a worker killed mid-poll, when 30 minutes pass, then the abandoned `source_poll` row is closed with an `abandoned` error and is no longer shown as the source's latest poll.
- [ ] Given an uploaded `.eml` with a `Received:` header dated 2019, when ingested, then `received_at` is clamped into the backfill window and the 7-day dedupe window behaves as if it arrived now.
- [ ] Given 30,000 messages in the trailing window, when the admin page renders, then `ingestion_metrics` completes in under 100 ms and does not load message bodies.
- [ ] Given an upload of 100 MiB, when posted, then the response is 413 and the process never holds the whole body.
- [ ] Given a ZIP whose central directory understates a member's uncompressed size, when extracted, then the total cap still holds and the member is flagged rather than decompressed without limit.

## Edge Cases and Required Tests

| Case | Expected | Test |
|---|---|---|
| Graph `_mime` fails for one item in a page | Delta link not stored; message retried next poll | `test_graph_cursor_holds_on_fetch_failure` |
| IMAP `FETCH` returns `NO` for a middle UID | `last_uid` stops below it; message retried | `test_imap_uid_holds_on_fetch_failure` |
| Same message fails past the attempt limit | `source_skips` row; cursor advances; review item | `test_poison_message_is_skipped_and_surfaced` |
| Attachment bytes exceed the batch cap mid-batch | Poll stops early, `more_available=True`, cursor unadvanced for the rest | `test_batch_byte_cap_stops_poll_early` |
| Single message over the batch cap | Processed alone, not skipped | `test_oversized_single_message_still_ingested` |
| Graph token expired | Refreshed from `expires_in`; one retry on 401 | `test_graph_token_refresh` |
| `config_enc` rotated between polls | Implementation rebuilt, new credentials used | `test_source_impl_rebuilt_on_config_change` |
| Concurrent insert of the same `Message-ID` | One row; `IntegrityError` converted to a duplicate link | `test_concurrent_dedupe_race` (`@pytest.mark.postgres`) |
| Two content-hash matches in the window | Earliest is the link target | `test_duplicate_link_target_is_deterministic` |
| `copies` vs `message_sources` | Always agree | `test_copies_matches_source_count` |
| Backfill job exhausts retries | Scheduler re-enqueues | `test_backfill_recovers_after_failed_job` |
| Backfill fails 20 times | `backfill_stuck`, visible in health | `test_backfill_stuck_is_surfaced` |
| `seed` raises | Poll row finalised, `status='error'`, no dangling row | `test_seed_failure_finalises_poll_row` |
| Source with `seed` but no `backfill` | Not seeded; history still ingested | `test_seed_requires_backfill_support` |
| Poll row abandoned by a dead worker | Closed after 30 min | `test_abandoned_poll_row_is_closed` |
| Forged far-past `Received:` header | Clamped into the backfill window | `test_received_at_is_clamped` |
| Transport supplies `INTERNALDATE`/`receivedDateTime` | Preferred over the `Received:` header | `test_transport_receipt_time_wins` |
| 30,000 messages in the window | Metrics under 100 ms, no body loads | `test_ingestion_metrics_at_volume` |
| Upload over the size cap | 413, body not fully read | `test_upload_size_cap` |
| ZIP with a lying `file_size` | Total cap enforced by a limited reader | `test_zip_declared_size_is_not_trusted` |
| ZIP with 50,000 entries | Listing truncated at 1,000, truncation noted | `test_zip_listing_is_bounded` |
| One bad alert recipient | Others still notified; alert not re-sent wholesale | `test_alert_is_per_recipient` |

Two existing tests encode the wrong behaviour and must be corrected as part of this work:

- `tests/test_ingestion_sources.py::test_imap_fetch_failure_does_not_advance_uid` asserts that the UID *does* advance past a failed fetch, under a name claiming the opposite. It becomes `test_imap_uid_holds_on_fetch_failure`.
- `tests/test_ingestion_pipeline.py::test_throughput` asserts a wall-clock rate and will flake on a loaded CI runner. Re-express it as a bounded-work assertion (query count and peak memory) with the timing kept only as a generous smoke bound.

## Technical Notes

- Phasing. **Phase 1** (data loss and availability): F1, F2, F3. **Phase 2** (integrity and recovery): F4, F5, F6. **Phase 3** (hygiene): F7, F8, F9, F10. Phase 1 is the only part that should block a production connection.
- The `PollResult` change in F1 is the one breaking interface change. `MailSource` and
  `BackfillableSource` in `ingestion/protocol.py` both move to generator + `commit_state`, and the
  four implementations plus their fakes change with them. Doing F1 and F2 together avoids
  rewriting the same call sites twice.
- **Alembic baseline is a blocking prerequisite.** `migrations/versions/` is empty, so none of the
  SPEC-01 columns — nor the constraints and indexes above — can reach a deployed database.
  `make dev` already runs `db-upgrade` as a no-op. The baseline must be generated against real
  Postgres, because `JSONType` resolves to `JSONB` there and to `JSON` elsewhere, and F4's partial
  unique index is Postgres-only. This is pre-existing debt, not introduced here, but SPEC-10 cannot
  ship without it.
- Bounded memory (F2) makes `ingest_live_batch` a count limit *and* `INGEST_MAX_BATCH_BYTES` a size
  limit. Keep both: the count bounds per-message overhead, the size bounds attachments.
- Minor cleanups to fold in: `sniff_mime`'s `\xd0\xcf\x11\xe0` branch returns `declared`
  unchanged on both paths and is dead code; `LocalBlobStore.put`'s temp file is named from the pid
  alone and collides between threads in one process; `S3BlobStore.exists` treats every `ClientError`
  (including permission denied) as "absent" and re-uploads.

## Metrics

- Messages lost to fetch failures per day: target 0, measured by `source_skips` growth.
- Worker RSS p95 during polling: target under 512 MB.
- Poll success rate after token-refresh work: target above 99.5 % excluding real outages.
- Duplicate `Message-ID` rows in `raw_messages`: target 0, asserted by a nightly check.
- `ingestion_metrics` p95 latency: target under 100 ms.
