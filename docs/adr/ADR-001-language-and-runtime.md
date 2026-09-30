# ADR-001: Python 3.12 modular monolith (FastAPI + background worker)

**Status:** Accepted · **Date:** 2026-09-29 · **Deciders:** Engineering lead, product

## Context

The system is an ETL pipeline (mail → text → structured record → score → rendered email) with a
thin web layer for review and admin. Workload is small (hundreds of messages/day), latency is
not user-facing except for the review pages, and the heaviest dependencies are document parsing
(PDF, DOCX, OCR) and an LLM SDK. Team is one to two engineers. Must be maintainable by whoever
inherits it.

## Decision

Python 3.12, one installable package `bidtriage`, run as two processes from the same image:
`api` (FastAPI + Jinja2) and `worker` (job runner + scheduler). Typed with Pydantic v2 models
throughout; `uv` for dependency management; `ruff` + `mypy` in CI.

## Options Considered

### Option A: Python modular monolith (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low: one language, one image, two entrypoints |
| Cost | Lowest |
| Scalability | Ample for 100× current volume by adding workers |
| Team familiarity | High |

**Pros:** best-in-class document parsing libraries (pypdf, pdfplumber, python-docx, pytesseract);
first-party Anthropic SDK with Pydantic structured outputs; Jinja2 for email templates; fast to
test pure functions (scoring, matching).
**Cons:** async story is less uniform than Node; needs discipline to keep module boundaries.

### Option B: TypeScript (Next.js + Node worker)
| Dimension | Assessment |
|---|---|
| Complexity | Medium: two runtimes if a Python sidecar is needed for OCR/PDF |
| Cost | Low |
| Scalability | Fine |
| Team familiarity | Medium |

**Pros:** richer UI toolkit if a dashboard becomes central.
**Cons:** document parsing and OCR are weaker; the UI advantage is irrelevant to an email-first product.

### Option C: Microservices (ingest, extract, score, notify)
**Pros:** independent scaling. **Cons:** four deployables for one customer and one engineer; every
failure mode becomes a distributed one. Rejected outright.

## Trade-off Analysis

The product's risk is in extraction quality and trust in scoring, not in throughput or UI polish.
Python minimizes the distance between the eval harness, the pipeline, and the tests.

## Consequences

- Easier: fixtures-driven testing, single deploy, one dependency lockfile.
- Harder: if a rich interactive board is later required, a separate front-end may be added (ADR-006 leaves room).
- Revisit: if volume exceeds ~10K messages/day or a second tenant appears.

## Action Items
1. [x] Scaffold `bidtriage` package with `api` and `worker` entrypoints.
2. [x] CI: ruff, mypy, pytest.
