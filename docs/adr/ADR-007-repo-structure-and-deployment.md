# ADR-007: Single repo, single Python package, one container image, managed Postgres

**Status:** Accepted · **Date:** 2026-09-29 · **Deciders:** Engineering lead

## Context

Follows from ADR-001/002/006. One customer, small team, must be deployable by a contractor.

## Decision

```
estimating-tool/
  docs/                  research, product, specs, adr, design, runbooks
  src/bidtriage/         the application package
    core/                config, db, models, jobs, clock, ids
    ingestion/           MailSource protocol; graph, imap, file sources; attachments; links
    extraction/          schema, prompts, ClaudeExtractor, FakeExtractor, postprocess
    resolution/          normalization, candidate generation, matching, merging, change tracking
    scoring/             profile schema, factors, engine, explanation
    gcs/                 GC directory, resolution, stats
    digest/              snapshot builder, renderers (html, text), sender
    decisions/           actions, tokens, state machine, outcomes, audit
    calendar/            ICS feed
    web/                 FastAPI app, routers, templates, static
    worker/              scheduler, job handlers
    cli.py               `bidtriage` command: db, sources, digest, eval
  tests/                 unit + integration; fixtures/messages/*.eml + .expected.json; golden/
  migrations/            alembic
  infra/                 Dockerfile, docker-compose.yml, deploy notes
  prompts/               versioned extraction prompts
  scripts/               seed data, corpus tooling
```

- One Docker image; `bidtriage api` and `bidtriage worker` as commands; env-var configuration
  (12-factor) with a typed `Settings` object.
- Deployment target: a single small VM or PaaS (Fly.io / Render / Azure Container Apps) with
  managed Postgres and an S3-compatible bucket. Azure is the natural fit if Ferry is on M365, but
  nothing is Azure-specific.
- CI: GitHub Actions running ruff, mypy, pytest with a Postgres service, and the extraction eval on
  fixtures.

## Options Considered

- **Monorepo with `apps/` and `packages/`**: right for a polyglot or multi-service system; here it adds ceremony around one package. Rejected.
- **Separate repos for pipeline and web**: doubles versioning pain for templates shared between digest and review pages. Rejected.
- **Serverless functions**: polling + OCR + long LLM calls fit poorly with function timeouts; cold starts irrelevant. Rejected.

## Consequences

- Easier: onboarding (one `make dev`), deploy (one image), testing (one `pytest`).
- Harder: package must keep clean module boundaries; enforced by import-linter contracts (`core` ← everything; `web`/`worker` are leaves; `scoring` and `resolution` import nothing from `web`/`digest`).

## Action Items
1. [x] Scaffold tree, `pyproject.toml`, `Makefile`, `docker-compose.yml`, Dockerfile, CI workflow.
2. [x] import-linter contracts.
