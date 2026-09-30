# Architecture Decision Records

| # | Title | Status |
|---|---|---|
| [ADR-001](ADR-001-language-and-runtime.md) | Python 3.12 modular monolith (FastAPI + worker) | Accepted |
| [ADR-002](ADR-002-datastore-and-queue.md) | PostgreSQL as system of record and job queue | Accepted |
| [ADR-003](ADR-003-email-ingestion.md) | Microsoft Graph polling with IMAP fallback; read-only | Accepted |
| [ADR-004](ADR-004-llm-extraction.md) | Claude structured outputs for classification and extraction | Accepted |
| [ADR-005](ADR-005-deterministic-scoring.md) | Deterministic, versioned rule-based scoring; no learned model in v1 | Accepted |
| [ADR-006](ADR-006-ui-strategy.md) | Email-first; server-rendered review pages (Jinja2 + HTMX); no SPA | Accepted |
| [ADR-007](ADR-007-repo-structure-and-deployment.md) | Single repo, single Python package, one container image, managed Postgres | Accepted |
| [ADR-008](ADR-008-opportunity-resolution.md) | Layered dedupe: hard keys, soft-key evidence score, human review band | Accepted |

Constraints common to all decisions: one customer, a few users, hundreds of messages a day, a
small engineering team (one to two people), part-time IT support at the customer, and a strong
preference for things that fail loudly over things that scale.
