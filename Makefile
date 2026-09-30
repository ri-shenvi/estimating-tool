.PHONY: dev sync test lint type lint-imports check api worker db-up db-revision db-upgrade eval-extraction calibrate digest-preview seed

sync:
	uv sync --all-groups

dev: sync db-up db-upgrade seed
	@echo "Run 'make api' and 'make worker' in two terminals."

test:
	uv run pytest

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

eval-extraction:
	uv run bidtriage eval-extraction tests/fixtures/messages

calibrate:
	uv run bidtriage calibrate tests/fixtures/labeled_corpus.csv

digest-preview:
	uv run bidtriage digest-preview --out var/digest-preview.html
