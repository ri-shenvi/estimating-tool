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
5. **Register the source**: Admin → Sources → Add Graph source (or `bidtriage add-source graph ...`),
   folders `Inbox`, backfill window 90 days. Click *Test connection*; expect the newest message subject.
6. **Sending identity** is separate: either SMTP relay credentials in `SMTP_URL` or a second app
   registration with `Mail.Send` restricted (same access-policy technique) to the `bids@` mailbox.
7. Verify within 15 minutes: Admin → Sources shows a poll with `seen > 0`; `/readyz` is 200.

Failure signatures: `AADSTS7000222` = expired secret (step 3); `ErrorAccessDenied` = access policy
(step 4); `410 Gone` on delta = normal token expiry, the worker resyncs automatically.
