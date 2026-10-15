# multi-tenant-agent

LangGraph + Pydantic AI agent that reads/toggles per-tenant page themes in MySQL,
running fully locally on an Ollama-served LLM (default `qwen3:8b`).

## Layout

```
app/
  core/       config (pydantic-settings), logging
  db/         SQLAlchemy models, session, seed
  services/   settings (resolution, validation, history, undo), admin, tenants
  agent/      Pydantic AI agent + tools, model warm-up
  workflow/   LangGraph tenant workflow
  api/        FastAPI app (/chat, /health, /ready)
migrations/   Alembic
tests/        pytest (SQLite + TestModel, no GPU/MySQL needed)
run_workflow.py   interactive CLI
```

## Data model

- **Reference:** `plans`, `features`, `plan_features`, `setting_keys` (type, allowed values,
  pattern, default, required feature), `templates`, `template_settings`.
- **Tenant data:** `tenants` (plan, template, status), `users` (role), `pages`, `settings`
  (override per page, or tenant-wide when `page_id` is NULL; optimistic `version`).
- **History:** `audit_log` (immutable; drives undo) and `outbox` (events for future
  consumers), both written in the same transaction as each change.
- `tenant_page_events` remains as a read-only MySQL view for old queries.

Effective value of a setting: **page override → tenant default → template → key default**.

| Setting       | Values                         | Notes                         |
|---------------|--------------------------------|-------------------------------|
| `theme`       | light, dark                    |                               |
| `language`    | en, es, fr, de                 |                               |
| `layout`      | comfortable, compact           |                               |
| `brand_color` | `#rrggbb`                      | needs `custom_branding` (pro) |
| `show_tips`   | true/false                     |                               |

## Agent tools

`list_pages`, `search_pages` (fuzzy), `get_theme`, `set_theme` (exact value, safe to
repeat; toggling = read then set), `get_settings_for_page`, `set_setting`,
`get_change_history`, `undo_last_change`. Tools return structured data. A wrong page,
key or value makes the model retry, with the valid options in the error.

Admin operations (`create_page`, `delete_page`, `bulk_set_setting`, `apply_template` in
`app/services/admin.py`) are not exposed to the agent until roles and confirmation exist.

## Stack (docker compose)

| Service       | Purpose                                      | Dev host port |
|---------------|----------------------------------------------|---------------|
| `app`         | FastAPI                                      | 8000          |
| `ollama`      | Local LLM server (GPU)                       | 11435         |
| `ollama-pull` | One-shot: pulls `$OLLAMA_MODEL`              | –             |
| `mysql`       | MySQL 8                                      | 3308          |
| `migrate`     | One-shot: `alembic upgrade head` (+ seed in dev) | –         |

On startup the app loads the model onto the GPU in the background, so the first
request isn't slow. `/ready` reports database and Ollama reachability.

## Development (auto-reload)

```bash
cp .env.example .env
docker compose up --build --watch
```

`compose.override.yml` is loaded automatically: code edits sync into the
container and uvicorn reloads; changes to requirements or the Dockerfile rebuild.

Checks (inside the dev image):

```bash
docker compose run --rm --no-deps app sh -c "ruff check . && ruff format --check . && mypy && pytest"
```

Optional git hooks: `pip install pre-commit && pre-commit install`.

## Production

```bash
docker compose -f compose.yml -f compose.prod.yml up -d --build
```

No reload, 2 uvicorn workers, restart policies and resource limits; only port
8000 is published.

## Use it

```bash
curl -X POST http://localhost:8000/chat -H "Content-Type: application/json" -d "{\"prompt\": \"What theme is the login page for tenant a1b2c3d4-e5f6-7890-abcd-ef0123456789?\"}"
```

Interactive docs: http://localhost:8000/docs · CLI: `docker compose exec app python run_workflow.py`

## Database migrations

```bash
docker compose run --rm migrate alembic revision -m "describe change"   # then edit migrations/versions/
docker compose run --rm migrate alembic upgrade head
```

To use your existing host MySQL instead of the container, set `DB_HOST:
host.docker.internal`, `DB_PORT: 3306` and your credentials in `compose.override.yml`.

## Updating dependencies

Edit `requirements.in` / `requirements-dev.in`, then regenerate the pinned lock files:

```bash
docker run --rm -v "$PWD:/src" -w /src python:3.12-slim sh -c "pip install -q uv && uv pip compile requirements.in -o requirements.txt && uv pip compile requirements-dev.in -o requirements-dev.txt"
```

## Configuration

See `.env.example`. Key settings: `OLLAMA_MODEL` (default `qwen3:8b`; alternatives
`llama3.1:8b`, `qwen3:14b` with more VRAM), `DOCKER_DB_PASSWORD`, `LOG_LEVEL`.

## GPU

Requires Docker Desktop with WSL2 + an NVIDIA driver. Without a GPU, remove the
`deploy:` block from the `ollama` service (CPU inference is much slower).
