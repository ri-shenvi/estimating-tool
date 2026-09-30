# Digest did not arrive

1. Open `/digests/<today>`; if it renders, the build succeeded and the problem is delivery:
   check the worker log for `send_digest` errors and the SMTP relay / Mailpit.
2. If 404: check the `jobs` table for `digest:<date>:<user>` rows — `failed` with `last_error`, or
   still `pending` with a future `run_at` (retries). Fix the cause; re-run with
   `bidtriage digest-preview` to confirm the build, then set the job back to `pending`.
3. If no job rows exist: the scheduler did not tick at 06:20. Is the worker running? Is
   `DIGEST_TIME`/`DIGEST_TIMEZONE` right? Was it a weekend or configured holiday?
4. Fallback: `bidtriage digest-preview --recipient <email>` and send the HTML manually while fixing.
