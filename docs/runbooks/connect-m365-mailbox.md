# Connect a Microsoft 365 mailbox (read-only)

Goal: the worker can read `estimating@ferryelectric.com` (and optionally estimator inboxes) with
no ability to send, move, flag or delete. See ADR-003 and SPEC-01.

1. **App registration** (Entra admin center → App registrations → New): name `bidtriage-mail-reader`,
   single tenant, no redirect URI. Note *Application (client) ID* and *Directory (tenant) ID*.
2. **API permission**: Microsoft Graph → *Application* permissions → `Mail.Read`. Grant admin consent.
   Do not add `Mail.ReadWrite` or `Mail.Send` to this registration.
3. **Client secret**: Certificates & secrets → New client secret, 12 months. Copy once; store in the
   `.env`/secret manager as `GRAPH_CLIENT_SECRET`. Put the expiry date in the team calendar.
4. **Scope to the mailbox** (Exchange Online PowerShell):
   ```powershell
   New-DistributionGroup -Name "bidtriage-mailboxes" -Type Security
   Add-DistributionGroupMember -Identity "bidtriage-mailboxes" -Member estimating@ferryelectric.com
   New-ApplicationAccessPolicy -AppId <client-id> -PolicyScopeGroupId bidtriage-mailboxes@ferryelectric.com -AccessRight RestrictAccess -Description "bidtriage read-only"
   Test-ApplicationAccessPolicy -Identity estimating@ferryelectric.com -AppId <client-id>   # expect Granted
   Test-ApplicationAccessPolicy -Identity someone.else@ferryelectric.com -AppId <client-id>  # expect Denied
   ```
5. **Register the source**:
   ```bash
   uv run bidtriage add-source graph \
     --name "Estimating mailbox" --mailbox estimating@ferryelectric.com --backfill-days 90 \
     --config-json '{"tenant_id":"<tenant>","client_id":"<client>","client_secret":"<secret>","folders":["inbox"]}'
   uv run bidtriage poll-sources     # one poll now, prints seen/new/duplicates
   uv run bidtriage source-health    # ok / degraded / down per source
   ```
   The secret is encrypted with `SECRET_KEY` before it is written to `sources.config_enc`.
   The first poll also queues the 90-day backfill, which runs at a lower priority than live polls
   (50 vs 80) so live mail is never starved; `bidtriage ingest-metrics` shows the lag.
6. **Sending identity** is separate: either SMTP relay credentials in `SMTP_URL` or a second app
   registration with `Mail.Send` restricted (same access-policy technique) to the `bids@` mailbox.
7. Verify within 15 minutes: Admin → Sources shows health `ok` and a poll with `seen > 0`;
   `/readyz` is 200. The source mailbox should show no change in read state, flags or folder —
   ingestion only ever issues Graph `GET`s (and IMAP `EXAMINE` + `BODY.PEEK` on the fallback).
8. **Forward-to address and manual upload**: a forward-to mailbox is registered exactly like any
   other source; forwarded mail is unwrapped so the original sender and date are used. One-off
   files go through Admin → Sources → Manual upload (`.eml` or `.msg`).

Failure signatures: `AADSTS7000222` = expired secret (step 3); `ErrorAccessDenied` = access policy
(step 4); `410 Gone` on delta = normal token expiry, the worker resyncs automatically.

A source with no successful poll for 30 minutes is `degraded` and for 60 minutes is `down`. Going
`down` sends one alert email (to `ADMIN_ALERT_TO`, else the admin and chief users) and shows in the
next digest's System health section; the alert re-arms only after the source recovers, so a long
outage does not mail every five minutes.
