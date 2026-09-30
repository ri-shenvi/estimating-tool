# bidtriage — working notes for agents

- Python 3.12, `uv`. Run everything with `uv run`. `make check` must pass before a commit.
- Read `docs/specs/SPEC-*.md` before changing a module; each spec lists acceptance criteria and the edge-case tests that must exist. Add the test when you add the behavior.
- Scoring (`scoring/engine.py`) and resolution (`resolution/`) are pure functions. Never add I/O there; assemble snapshots in `worker/pipeline.py`.
- The LLM only extracts. Dates are enforced by `extraction/postprocess.py`; do not let model output bypass it.
- Extraction prompt and schema are versioned together (`prompts/extract_vN.md`, `extraction/schema.py`). Bumping either requires updating `tests/fixtures/messages/*.expected.json` and running `bidtriage eval-extraction --offline`.
- Anthropic SDK 1.x: `client.messages.parse(..., output_format=PydanticModel)`, model `claude-opus-5-5`, `fallbacks="default"` with beta `server-side-fallback-2026-07-01`. No `budget_tokens`, no prefill, no forced `tool_choice`.
- Tests run on SQLite; anything Postgres-only (SKIP LOCKED, pg_trgm) must degrade gracefully or be marked `@pytest.mark.postgres`.
- Timestamps: store UTC, reason in `America/New_York` via `core/clock.py`; use `aware()` on datetimes read from SQLite.
- Import contracts are enforced by import-linter (see `pyproject.toml`).
