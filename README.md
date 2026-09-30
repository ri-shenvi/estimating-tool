# bidtriage

ITB triage for [Ferry Electric Company](https://www.ferryelectric.com): reads incoming invitations
to bid from the estimating mailbox, scores fit by project type, size and general contractor, and
sends the estimators a short morning digest of scope, bid dates and changes.

**Status:** scaffold with working pipeline on fixtures. Not yet connected to a live mailbox.

## Documents

| Area | Where |
|---|---|
| How electrical subs receive and triage bids (research, sourced) | [`docs/research/bidding-process.md`](docs/research/bidding-process.md) |
| Brainstorm record and chosen direction | [`docs/product/00-brainstorm.md`](docs/product/00-brainstorm.md) |
| Product requirements (PRD) | [`docs/product/01-prd.md`](docs/product/01-prd.md) |
| Feature specs with acceptance criteria and edge-case tests | [`docs/specs/`](docs/specs/) (SPEC-01 … SPEC-09) |
| Architecture decisions | [`docs/adr/`](docs/adr/) |
| System design (components, data model, jobs, failure modes) | [`docs/design/system-design.md`](docs/design/system-design.md) |
| Runbooks | [`docs/runbooks/`](docs/runbooks/) |

## How it works

```
mailbox (Graph / IMAP / forward-to) ──► raw_messages + attachments (read-only, idempotent)
      ──► Claude structured extraction (kind, GC, dates, scope, size, flags; per-field source excerpts)
      ──► opportunity resolution (dedupe across channels, addenda, date-change diffs)
      ──► deterministic fit score 0–100 from a versioned profile (type, size, GC, distance, timing, bid type)
      ──► 06:30 digest email with one-click Bid / Pass / Assign / Snooze links
      ──► decisions and outcomes feed GC stats and future scoring
```

One Python package, two processes (`api`, `worker`), one Postgres, one object bucket. See ADR-001, 002, 007.

## Quick start (local, no Docker required for the demo path)

```bash
# 1. toolchain
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --all-groups

# 2. demo on SQLite with fixture-backed extraction (no API key needed)
export DATABASE_URL=sqlite:///var/dev.db SECRET_KEY=$(openssl rand -hex 32)
uv run bidtriage db-create
uv run bidtriage seed-gcs
uv run bidtriage add-user you@ferryelectric.com "Your Name" --role chief
uv run bidtriage ingest-dir tests/fixtures/messages --fake
uv run bidtriage digest-preview --out var/digest-preview.html   # open in a browser
uv run bidtriage api                                           # http://localhost:8000
```

Full stack with Postgres and a local mail catcher (Mailpit at http://localhost:8025):

```bash
cp .env.example .env            # fill ANTHROPIC_API_KEY for live extraction
docker compose -f infra/docker-compose.yml up -d postgres mailpit
uv run alembic upgrade head
uv run bidtriage seed-gcs
uv run bidtriage api            # terminal 1
uv run bidtriage worker         # terminal 2 (add --fake-fixtures tests/fixtures/messages to avoid API calls)
```

## Commands

| Command | Purpose |
|---|---|
| `make check` | ruff + mypy + import-linter + pytest |
| `bidtriage ingest-dir DIR [--fake]` | Run .eml/.msg files through the whole pipeline synchronously |
| `bidtriage add-source KIND --name ... --config-json ...` | Register a Graph/IMAP/file mail source; credentials are encrypted with `SECRET_KEY` |
| `bidtriage poll-sources` | Poll every active source once; prints seen/new/duplicates per source |
| `bidtriage source-health` | `ok` / `degraded` / `down` per source (SPEC-01 F8 thresholds) |
| `bidtriage ingest-metrics` | Poll success rate, ingestion lag p50/p95, attachment and duplicate counts |
| `bidtriage ingest-skips` | Messages ingestion gave up on, so a gap is never silent |
| `bidtriage digest-preview` | Build the digest without sending |
| `bidtriage eval-extraction tests/fixtures/messages [--offline]` | Per-field extraction accuracy against `.expected.json` |
| `bidtriage calibrate labeled.csv` | Precision/recall of the Bid band against the chief estimator's labels |

## Repository layout

```
src/bidtriage/
  core/        settings, ORM models, Postgres job queue, clock, crypto, SHA-256 blob store
  ingestion/   .eml/.msg parsing, forward unwrapping, attachment text, links, source health; Graph / IMAP / file sources
  extraction/  schema, prompt loader, Claude extractor, fixture extractor, deterministic post-processing
  resolution/  name normalization, evidence scoring, date-merge rules
  scoring/     profile schema + defaults, pure scoring engine with explanations
  gcs/         GC directory resolution, seed list, stats
  digest/      snapshot assembly, HTML/text templates, SMTP sender
  decisions/   action state machine, signed one-click tokens, apply_action + audit
  calendar/    ICS feed
  web/         FastAPI app, routers, Jinja templates
  worker/      pipeline glue, job handlers, scheduler, digest job
prompts/       versioned extraction prompts
tests/         unit, pipeline and web tests; fixtures/messages/*.eml + .expected.json
docs/          research, product, specs, adr, design, runbooks
infra/         Dockerfile, docker-compose, init.sql
migrations/    alembic
```

## What is real and what is scaffolded

Working and tested: parsing, dedupe, forward unwrapping, attachment text, link harvesting,
post-processing date rules, cross-channel opportunity resolution, addenda and date-change history,
scoring with explanations, digest assembly and rendering, signed action links with pre-fetch
safety, state machine and audit, ICS feed, job queue semantics, seed GC directory, web routes.

Written but not exercised against live systems: `GraphSource`, `ImapSource`, `ClaudeExtractor`,
SMTP sending. Authentication is a dev-mode shim; Entra ID OIDC must be wired before any non-dev
deployment (see `web/deps.py` and SPEC-09).

Not built yet (specified): profile editor form with preview (SPEC-07 F6; JSON editor exists),
review queue page with merge/split (SPEC-03 F6), outcome prompts in the digest (SPEC-06 F4),
auto-reply (P1), Teams delivery (P1), retention job, `/metrics`.
