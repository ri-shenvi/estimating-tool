# ADR-006: Email-first; server-rendered review pages (Jinja2 + HTMX); no SPA

**Status:** Accepted · **Date:** 2026-09-29 · **Deciders:** Product, engineering lead

## Context

The users asked for a morning summary, not a dashboard. Competitors are dashboards. The review
page needs forms, tables, filters and a handful of actions; it does not need real-time
collaboration or complex client state. The team is small.

## Decision

- Primary surface: the HTML digest email with signed action links (SPEC-05/06).
- Secondary surface: server-rendered pages from FastAPI with Jinja2 templates and HTMX for partial
  updates; every action works without JavaScript; the same templates power the email (with an
  email-safe subset).
- No separate front-end build, no SPA framework, no client-side router.

## Options Considered

### Option A: Server-rendered + HTMX (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Cost | Low |
| Scalability | Fine |
| Team familiarity | High |

**Pros:** one language, one deploy, progressive enhancement, trivial auth integration, fast to build forms.
**Cons:** less slick for a future kanban board.

### Option B: Next.js/React front-end + JSON API
**Pros:** best path to a rich board. **Cons:** doubles the surface area; the board is a non-goal.

### Option C: No web UI at all (email + CSV)
**Pros:** simplest. **Cons:** profile editing, merges, and outcome capture need forms. Rejected.

## Consequences

- Easier: shipping; security review (no token-in-browser SPA auth).
- Harder: if the board becomes necessary, add a React island or a separate app against the same API; the JSON API is kept clean for that reason.
- Revisit: if estimators ask for a board after 90 days of digest use.

## Action Items
1. [x] FastAPI app with Jinja2 templates, HTMX vendored, base layout.
2. [x] JSON API alongside HTML routes for future clients.
