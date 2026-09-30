.PHONY: dev sync test test-postgres lint type lint-imports check api worker db-up db-revision db-upgrade eval-extraction validate-fixtures calibrate digest-preview seed

sync:
	uv sync --all-groups

dev: sync db-up db-upgrade seed
	@echo "Run 'make api' and 'make worker' in two terminals."

test:
	uv run pytest

# The `postgres`-marked tests: JSONB variants, the partial unique index and the pg_trgm candidate
# search, none of which SQLite can express. Skipped automatically when no server is reachable;
# point BIDTRIAGE_TEST_POSTGRES_URL elsewhere to use a different one.
test-postgres:
	uv run pytest -m postgres -v

lint:
	uv run ruff check . && uv run ruff format --check .

type:
	uv run mypy

lint-imports:
	uv run lint-imports

check: lint type lint-imports test

api:
	uv run uvicorn bidtriage.web.app:app --reload --port 8000

worker:
	uv run bidtriage worker

db-up:
	docker compose -f infra/docker-compose.yml up -d postgres

db-revision:
	uv run alembic revision --autogenerate -m "$(m)"

db-upgrade:
	uv run alembic upgrade head

seed:
	uv run bidtriage seed-gcs

# Calls the model once per fixture and gates on the SPEC-02 accuracy goals. Costs money, needs a
# key; run it after a prompt or schema change, and on a schedule (.github/workflows/extraction-eval.yml).
eval-extraction:
	uv run bidtriage eval-extraction tests/fixtures/messages

# What CI runs on every push: is the corpus valid and does post-processing hold the F2 invariants.
# This measures the fixtures, not accuracy — see the note in extraction/evaluate.py.
validate-fixtures:
	uv run bidtriage eval-extraction tests/fixtures/messages --offline

calibrate:
	uv run bidtriage calibrate tests/fixtures/labeled_corpus.csv

digest-preview:
	uv run bidtriage digest-preview --out var/digest-preview.html
