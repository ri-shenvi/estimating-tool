# SPEC-01: Mailbox and Attachment Ingestion

**Status:** Implemented · **Priority:** P0 · **Depends on:** none · **Feeds:** SPEC-02

> A review of the implementation found defects this spec's tests do not catch — silent message loss
> on transient fetch failures, unbounded worker memory, an unrefreshed Graph token, unenforced
> deduplication. They are fixed under [SPEC-10](SPEC-10-ingestion-hardening.md), which also replaces
> the source interface described below: sources now stream a `FetchSession` and the poller decides
> when a cursor may advance.

## Problem

ITBs arrive in a shared estimating mailbox, in estimators' personal inboxes, and as PDF letters
attached to short emails. Nothing downstream works unless every relevant message is captured
exactly once, with its attachments, without touching the source mailbox.

## Goals

- Capture 100% of messages in the connected mailbox folder(s) and the forward-to address, including attachments, within 15 minutes of arrival.
- Never modify, move, flag, or delete source mail.
- Idempotent: re-running ingestion over the same mail produces no duplicate records.
- Degrade loudly: any connection failure longer than 60 minutes is visible in the digest and the health page.

## Non-Goals

- Fetching documents behind platform logins (BuildingConnected, Procore plan rooms). v2.
- Parsing drawing sets. Only ITB letters, scope sheets, bid forms, and addenda documents are text-extracted.
- Real-time (webhook) delivery. Polling every 5 minutes is sufficient at Ferry's volume.

## Functional Requirements

### F1. Sources

| Source | Mechanism | Config |
|---|---|---|
| Microsoft 365 mailbox (primary) | Graph API, application permission `Mail.Read` scoped to the mailbox, delta query per folder | tenant, client id/secret, mailbox UPN, folder list (default: Inbox) |
| IMAP mailbox (fallback) | IMAP IDLE not required; poll with UID ranges per folder | host, port, TLS, user, app password, folder list |
| Forward-to address | A dedicated mailbox (e.g. `itb@bidtriage.ferryelectric.com` or a Ferry alias) polled like any other; the *forwarded* message is unwrapped so the original sender and date are used | same as above |
| Manual upload | Admin page: drop an `.eml` or `.msg` file | none |

### F2. Message record

Each captured message is stored as a `raw_message` with: source id, provider message id, internet
`Message-ID`, `In-Reply-To`, `References`, from, to, cc, subject, sent and received timestamps,
plain-text body, HTML body, header dump, and a content hash. Attachments are stored as
`raw_attachment` rows with filename, MIME type, size, SHA-256, blob location, and extracted text
(when extractable).

### F3. Deduplication at ingestion

A message is a duplicate and is *linked, not re-created* when any of these match an existing record:
1. Same provider message id from the same source.
2. Same internet `Message-ID` header.
3. Same content hash (normalized subject + normalized body + sorted attachment hashes) received within 7 days.

Linked duplicates increment a `copies` counter and record the additional recipient path (this
captures "GC CC'd four people" fan-out without creating four opportunities).

### F4. Forward unwrapping

When a message arrives at the forward-to address or is detected as a forward (subject prefix
`FW:`/`Fwd:`, or an attached `message/rfc822` part), the original message is reconstructed:
original sender, original date, original subject and body. The forwarding user is recorded as
`forwarded_by` and becomes the default assignee suggestion downstream. The forwarder's own
commentary above the forward block is preserved as `forward_note`.

### F5. Attachment text extraction

| Type | Handling |
|---|---|
| PDF (text layer) | Extract text per page; keep page numbers |
| PDF (scanned, no text layer) | OCR up to 20 pages; mark `ocr=true` |
| DOCX / XLSX | Extract text; XLSX limited to first 5 sheets, 500 rows each |
| Images in body (signature logos) | Ignored |
| ZIP | List entries; extract contained PDFs/DOCX up to 25 files and 200 MB total; deeper nesting ignored |
| Drawing sets (PDF > 40 pages or > 50 MB) | Stored, not text-extracted; flagged `large_document` |
| Anything else | Stored, `text=null` |

### F6. Link harvesting

All URLs in body and attachments are extracted and classified by host: `buildingconnected`,
`procore`, `isqft`/`constructconnect`, `planhub`, `smartbid`, `pantera`, `dropbox`, `box`,
`sharefile`, `egnyte`, `onedrive`/`sharepoint`, `google drive`, `other`. Tracking-wrapped links
(SendGrid, Mailchimp, SafeLinks) are unwrapped when the target is in the query string; otherwise
kept as-is and marked `wrapped=true`.

### F7. Scheduling and state

- Poll every 5 minutes per source. Graph uses delta tokens per folder; IMAP uses last-seen UID per folder.
- Backfill on first connection: configurable window, default 90 days, processed oldest-first, rate-limited so live mail is not starved (backfill batch of 50 between live polls).
- Every poll writes a `source_poll` row: started, finished, messages seen, new, duplicates, errors.

### F8. Health

- A source is `degraded` if the last successful poll is older than 30 minutes and `down` if older than 60.
- `down` sources appear in the digest's System health section and trigger an admin alert email.

## Acceptance Criteria

- [x] Given a connected Graph mailbox with 3 new messages, when the poller runs, then 3 `raw_message` rows exist with bodies, headers, and attachments, and the source mailbox shows no change in read state, flags, or folder.
- [x] Given the same 3 messages, when the poller runs again, then no new rows are created and the poll row shows `new=0, duplicates=3`.
- [x] Given one ITB sent to `estimating@` with CC to two estimators whose inboxes are also connected, when all three sources are polled, then exactly one `raw_message` exists with `copies=3` and three recipient paths.
- [x] Given an estimator forwards a GC email to the forward-to address with the note "Casey, worth a look?", when ingested, then the record's `from` is the GC's address, `sent_at` is the original date, `forwarded_by` is the estimator, and `forward_note` equals the note.
- [x] Given a 6-page text PDF ITB letter attached, when ingested, then `raw_attachment.text` contains the letter text with page markers.
- [x] Given a 120-page, 80 MB drawing set attached, when ingested, then the attachment is stored, `text` is null, and `large_document=true`.
- [x] Given a scanned 2-page PDF with no text layer, when ingested, then OCR text is present and `ocr=true`.
- [x] Given a ZIP with 3 PDFs and 1 nested ZIP, when ingested, then the 3 PDFs are extracted and the nested ZIP is listed but not opened.
- [x] Given a Microsoft SafeLinks-wrapped BuildingConnected URL, when links are harvested, then the unwrapped URL is stored with host class `buildingconnected`.
- [x] Given the Graph token is revoked, when 60 minutes pass, then the source status is `down`, an admin alert is sent once (not every 5 minutes), and the next digest shows the outage.
- [x] Given a 90-day backfill of 4,000 messages, when live mail arrives during backfill, then live messages are ingested within 15 minutes.

## Edge Cases and Required Tests

| Case | Expected | Test |
|---|---|---|
| Message with no plain-text part, HTML only | Body text derived from HTML with links preserved | `test_html_only_body` |
| Message with `Message-ID` missing | Fall back to provider id and content hash | `test_missing_message_id` |
| Same `Message-ID` from two different providers (M365 and IMAP of the same mailbox) | One record, two source paths | `test_cross_provider_dedupe` |
| Two different messages with identical subject and body but different attachments | Two records (attachment hashes differ) | `test_hash_includes_attachments` |
| Forward of a forward (FW: FW:) | Innermost original is used; each forwarder recorded in order | `test_nested_forward` |
| Forward where Outlook inlines the original as text, not RFC822 | Header block parsed from the text ("From: ... Sent: ... Subject: ...") | `test_inline_forward_headers` |
| Encrypted or password-protected PDF | Stored; `text=null`; `extraction_error="encrypted"` | `test_encrypted_pdf` |
| Attachment named `.pdf` but content is HTML (portal redirect) | Detected by magic bytes; treated as `other` | `test_mime_sniff` |
| Attachment > 200 MB | Metadata stored; blob skipped; flagged `oversize` | `test_oversize_attachment` |
| Email body 2 MB of quoted history | Body stored whole; `body_trimmed` field keeps only content above the first quoted reply marker | `test_quoted_history_trim` |
| Timestamps without timezone (IMAP `Date` header malformed) | Received time from server used; `sent_at_confidence=low` | `test_bad_date_header` |
| Graph delta token expired (410 Gone) | Full resync of the folder; no duplicates created | `test_delta_token_reset` |
| IMAP UIDVALIDITY changes | Folder re-scanned from UID 1 with dedupe by `Message-ID` | `test_uidvalidity_change` |
| Poll crashes mid-batch | Partial batch is committed per message; rerun completes without dupes | `test_crash_resume` |
| Mailbox contains 30,000 non-ITB newsletters | All captured (classification is SPEC-02's job); ingestion throughput ≥ 5 msgs/sec | `test_throughput` |
| Character sets: Windows-1252 subject, UTF-8 body with emoji | Decoded without mojibake | `test_charsets` |
| Attachment filename with path separators or null bytes | Sanitized before blob write | `test_filename_sanitize` |

## Implementation

| Requirement | Code |
|---|---|
| F1 sources | `ingestion/graph_source.py`, `ingestion/imap_source.py`, `ingestion/file_source.py`, `ingestion/msg.py` (`.msg` upload), `worker/sources.py` (build from an encrypted `sources` row) |
| F2 message record | `ingestion/eml.py`, `core/models.py` (`RawMessage`, `RawAttachment`, `MessageSource`, `MessageLink`) |
| F3 dedupe | `worker/pipeline.py::find_duplicate` / `ingest_parsed` |
| F4 forward unwrapping | `ingestion/eml.py::_unwrap_rfc822` / `_unwrap_inline` |
| F5 attachment text | `ingestion/attachments.py`, blobs in `core/blobs.py` |
| F6 link harvesting | `ingestion/links.py`, called on body *and* attachment text from `ingest_parsed` |
| F7 scheduling, backfill | `worker/ingest_job.py::run_poll` / `run_backfill`, `worker/handlers.py::schedule_tick` |
| F8 health, alerting | `ingestion/health.py`, `worker/ingest_job.py::check_sources` |
| Metrics | `worker/ingest_job.py::ingestion_metrics`, `bidtriage ingest-metrics`, admin page |

Operator commands: `bidtriage add-source`, `poll-sources`, `source-health`, `ingest-metrics`,
`ingest-dir`. Setup is in `docs/runbooks/connect-m365-mailbox.md`.

## Technical Notes

- Blob storage: local disk in dev, S3-compatible bucket in production, addressed by SHA-256 so duplicate attachments across messages are stored once.
- OCR via Tesseract; time-box 60 seconds per document; failure is non-fatal.
- Graph application permissions must be restricted to the target mailbox with an application access policy. Document this in the runbook.

## Metrics

- Poll success rate, ingestion lag p50/p95, attachments extracted vs. failed, duplicates linked per day.
