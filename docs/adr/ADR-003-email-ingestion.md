# ADR-003: Microsoft Graph polling with IMAP fallback; read-only access

**Status:** Accepted · **Date:** 2026-09-29 · **Deciders:** Engineering lead, Ferry IT (to confirm tenant)

## Context

ITBs arrive by email. Ferry is assumed to be on Microsoft 365 (unconfirmed). The mailbox must
never be altered by the tool. Latency requirement is "within 15 minutes." Some ITBs go to personal
inboxes.

## Decision

- Primary: Microsoft Graph with application permission `Mail.Read`, restricted by an Exchange
  application access policy to the estimating mailbox(es); folder delta queries every 5 minutes.
- Fallback: IMAP with an app password, UID-based polling, same 5-minute cadence.
- Escape hatch: a forward-to address (any mailbox the tool polls) with forward unwrapping.
- Sending (digest, alerts, optional auto-replies) is a separate concern using SMTP or Graph
  `Mail.Send` scoped to the sending mailbox only.

## Options Considered

### Option A: Graph delta polling (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low-medium (app registration, access policy) |
| Cost | None |
| Scalability | Fine |
| Team familiarity | Medium |

**Pros:** reliable change tracking, attachments via API, tenant-native auth, read-only scoping.
**Cons:** requires tenant admin consent; delta tokens expire and must be reset.

### Option B: Graph change notifications (webhooks)
**Pros:** near-real-time. **Cons:** public endpoint, subscription renewal, and no benefit at a 15-minute SLA. Deferred.

### Option C: Mail rule forwarding everything to the tool's own mailbox
**Pros:** no tenant integration. **Cons:** modifies mail flow, loses original recipient context, admins dislike it. Kept only as the manual forward-to escape hatch.

### Option D: Direct platform APIs (BuildingConnected, Procore)
**Pros:** structured data. **Cons:** each covers only its own invitations; API access is uneven; direct GC email would still need parsing. Considered for v2 document retrieval, not ingestion.

## Consequences

- Easier: uniform pipeline regardless of channel; strict read-only posture.
- Harder: platform-hosted documents remain behind logins (v2).
- Revisit: if Ferry is on Google Workspace, implement a Gmail API adapter behind the same `MailSource` interface.

## Action Items
1. [x] `MailSource` protocol with `GraphSource`, `ImapSource`, `FileSource` (tests).
2. [ ] Runbook: app registration + application access policy.
