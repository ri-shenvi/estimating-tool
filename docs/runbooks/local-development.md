# Local development

1. `curl -LsSf https://astral.sh/uv/install.sh | sh` then `uv sync --all-groups`.
2. Fast path (SQLite, no API key): see README "Quick start". Use `--fake` / `--fake-fixtures` flags so
   extraction reads `tests/fixtures/messages/*.expected.json`.
3. Postgres path: `docker compose -f infra/docker-compose.yml up -d postgres mailpit`, then
   `uv run alembic upgrade head`. Mailpit shows sent digests at http://localhost:8025.
4. Live extraction: set `ANTHROPIC_API_KEY` (or `ant auth login`). Cost at fixture volume is cents.
5. `make check` before every commit. Golden digest HTML lives in `var/digest-preview.html` after
   `bidtriage digest-preview`; open it in Outlook desktop and iOS Mail when templates change.
6. Adding a fixture: drop `name.eml` and `name.expected.json` in `tests/fixtures/messages/`; the
   pipeline test and `eval-extraction --offline` pick it up automatically.
