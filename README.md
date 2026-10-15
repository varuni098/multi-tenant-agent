# multi-tenant-agent

LangGraph + Pydantic AI agent that reads and changes per-tenant page settings in MySQL,
running fully locally on an Ollama-served LLM (default `qwen3:8b`), behind an
authenticated, tenant-pinned harness.

## Layout

```
app/
  core/       config (pydantic-settings), logging
  db/         SQLAlchemy models, session, seed
  services/   settings (resolution, validation, history, undo), admin, tenants
  harness/    auth (JWT), RBAC policy, guardrails, intent, rate limits, session
              memory, model reliability, state store, tenant-mcp client
  mcp_server/ tenant-mcp: MCP tools, caller auth, confirmations, internal routes
  graph/      knowledge graph: ontology loader, projection, validator, drift,
              rules, queries, outbox relay, sync worker, reconciler
  graph_mcp/  graph-mcp: read-only graph tools + the can_apply write gate
ontology/     ontology.yaml (classes, relationships, cardinality, rules, aliases)
  agent/      Pydantic AI agent + tools, model warm-up
  workflow/   LangGraph request flow
  api/        FastAPI app (/v1/chat, /v1/chat/confirm, /health, /ready)
migrations/   Alembic
tests/        pytest (SQLite + scripted FunctionModel, no GPU/MySQL/Redis needed)
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

## Architecture

```
client --JWT--> app (harness + agent) --MCP--> tenant-mcp --> MySQL --outbox--> outbox-relay --> RabbitMQ
                  |   |   \--MCP + can_apply gate--> graph-mcp --> Neo4j <-- graph-sync <------/
                  |   \--> Ollama (qwen3:8b)                       (re-projects tenants, reconciles)
                  \--> Redis                     external MCP clients (user JWT) -> tenant-mcp / graph-mcp
```

- **tenant-mcp** (`app/mcp_server/`) is the only service with database credentials, and
  the only one on the `data` network with MySQL. It serves the 12 tools over MCP
  (streamable HTTP) and two internal routes the app uses: `/internal/whoami` and
  `/internal/confirm`.
- **The agent app** has no database access. Every call to tenant-mcp carries a signed
  `X-Tenant-Context` (tenant, user, role, purpose, request ID; HS256 with
  `MCP_CONTEXT_SECRET`). The server verifies it and re-checks membership, tenant status
  and role in the database.
- **Purposes** bind credentials to endpoints. `auth` is for whoami, `agent` for MCP
  tools, and `confirm` for executing a confirmation. An agent run can never approve its
  own gated action.
- **Every MCP call** is logged server-side in `tool_calls` (actor, channel, tool,
  redacted args, outcome, duration, request ID), including denied and failed calls.
- **Tool annotations** (`readOnlyHint`, `destructiveHint`, `idempotentHint`) come from the
  same policy table the harness uses (`app/harness/policy.py`).

## Knowledge graph

**Ontology** (`ontology/ontology.yaml`, version 1) is the single source of truth for the
graph's shape:

- **Classes:** Tenant, User, Page, Setting, ChangeEvent (tenant-scoped); Role, Action, Plan,
  Feature, SettingKey, Template (shared). DocChunk and Concept are declared for Phase 6.
- **Relationships with cardinality:** MEMBER_OF, HAS_ROLE, MAY, SUBSCRIBES_TO, INCLUDES,
  REQUIRES, INHERITS_FROM, DEFINES, OWNS, HAS_SETTING, OF_KEY, CHANGED.
- **Rules,** in the order `can_apply` evaluates them: `tenant_active`, `permission`,
  `known_setting`, `allowed_value`, `entitlement`.
- **Inheritance order:** page, then tenant, then template, then default.
- **Aliases** for entity resolution ("accent colour" -> `brand_color`).

**Sync.** Every change writes an outbox row in the same transaction. `outbox-relay`
publishes it to RabbitMQ (exchange `tenant.events`, at-least-once), and `graph-sync`
re-reads that tenant from MySQL and replaces its subgraph in one Neo4j transaction:

- That makes events idempotent and safe to receive out of order.
- Failed messages go to `graph-sync.dlq`.
- `graph-sync` also reconciles at startup and every `RECONCILE_INTERVAL_SECONDS`
  (default 3600), repairing any drift.
- Measured lag from commit to graph is about 2 s.

**Graph tools** (`graph-mcp`, read-only, tenant-pinned, fixed Cypher only):

| Tool | Agent | Notes |
|---|---|---|
| `explain_setting` | viewer+ | why a value is what it is, with the inheritance chain |
| `can_apply` | viewer+ | allowed or denied, the deciding rule, and the graph path |
| `impact_of_change` | admin | what a template switch would change on this tenant's pages |
| `graph_effective_settings`, `resolve_entity`, `find_related` | external clients only | `find_related` paths never cross into another tenant |

**Write gate.** With `GRAPH_MODE=enforce` (the compose default), the harness asks
`can_apply` before any `set_theme`, `set_setting`, `bulk_set_setting` or `create_page`
reaches tenant-mcp:

- A denial is returned to the model with the ontology rule, and listed in the response's
  `denials[]`.
- If the policy engine is unreachable, writes are refused (fail closed).
- `advisory` only records denials; `off` disables the graph entirely. tenant-mcp still
  validates every write regardless.

**Operations:**

```bash
docker compose exec graph-sync python -m app.graph.validate           # graph vs ontology; exit 1 on violations
docker compose exec graph-sync python -m app.graph.reconcile --check  # drift report; exit 1 on drift
docker compose exec graph-sync python -m app.graph.reconcile          # repair drift
```

Neo4j Browser: http://localhost:7475 (dev) · RabbitMQ UI: http://localhost:15673 (user `agent`)

## Agent tools

No tool takes a tenant argument. The tenant and user come from the verified token,
through the signed service context. Tools return structured data. A wrong page, key or value
makes the model retry, with the valid options in the error.

| Tool | Role | Notes |
|---|---|---|
| `list_pages`, `search_pages`, `get_theme`, `get_settings_for_page` | viewer | |
| `get_change_history` | editor | |
| `set_theme`, `set_setting`, `undo_last_change` | editor | `set_theme` sets an exact value (toggle = read then set) |
| `create_page` | admin | |
| `delete_page`, `bulk_set_setting`, `apply_template` | admin | **confirmation required** |

## Harness

Request flow (`app/workflow/graph.py`):

```
JWT -> rate limit -> check_tenant -> guard_input -> classify -> run_agent | off_topic
```

- **Identity:** a JWT with `sub` (email), `tenant_id` and `role`, signed HS256 with
  `JWT_SECRET` or RS256 via `JWT_JWKS_URL` (OIDC). The user must exist in that tenant,
  and the effective role is the lower of the token role and the database role.
- **Tenant pinning:** tools read the tenant from `deps`, never from model arguments. A
  prompt that names another tenant's UUID is refused before it reaches the model.
- **Role-based access:** the model is only offered the tools the role allows. Read-only
  requests (by keyword intent) lose every write tool, even for admins. Every call is
  re-checked inside the tool as well.
- **Input guardrails:** empty or overlong prompts and prompt-injection patterns are refused;
  off-topic prompts get a help reply without calling the model. Logs redact emails,
  tokens and long numbers.
- **Write gate:** destructive and bulk actions are validated and summarised, then stored
  under a single-use token (5 minutes) bound to the tenant and user. `POST
  /v1/chat/confirm` runs the stored call directly, with no model involved.
- **Output check:** an answer that claims a change when none happened is retried once,
  then replaced with a safe reply. `actions[]` in the response is the ground truth.
- **Reliability:** per-call timeout, request and tool-call limits per run, a circuit
  breaker per model, and an optional `OLLAMA_FALLBACK_MODEL`.
- **Sessions:** history per tenant, user and `session_id` (last 10 turns, 1 h).
  Redis-backed in compose, in-memory otherwise.
- **Rate limits:** per user and per tenant per minute (HTTP 429).

## Stack (docker compose)

| Service       | Purpose                                      | Dev host port |
|---------------|----------------------------------------------|---------------|
| `app`         | FastAPI                                      | 8000          |
| `ollama`      | Local LLM server (GPU)                       | 11435         |
| `ollama-pull` | One-shot: pulls `$OLLAMA_MODEL`              | –             |
| `mysql`       | MySQL 8                                      | 3308          |
| `tenant-mcp`  | MCP tool server; only service with DB access | 8001          |
| `graph-mcp`   | Graph tools + can_apply gate (Neo4j only)    | 8002          |
| `neo4j`       | Knowledge graph                              | 7475 / 7688   |
| `rabbitmq`    | Event broker (outbox -> graph-sync)          | 15673 (UI)    |
| `outbox-relay`| MySQL outbox -> RabbitMQ                     | –             |
| `graph-sync`  | RabbitMQ -> Neo4j projection, reconciler     | –             |
| `redis`       | Sessions, rate limits, confirmations         | –             |
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

Checks (inside the dev image; unit tests need no Neo4j/RabbitMQ):

```bash
docker compose run --rm --no-deps app sh -c "ruff check . && ruff format --check . && mypy && pytest"
```

Integration tests (throwaway Neo4j + RabbitMQ):

```bash
docker compose --profile test run --rm tests
```

Optional git hooks: `pip install pre-commit && pre-commit install`.

## Production

```bash
docker compose -f compose.yml -f compose.prod.yml up -d --build
```

No reload, 2 uvicorn workers, restart policies and resource limits; only port
8000 is published. `APP_ENV=prod` requires a real `JWT_SECRET` (or `JWT_JWKS_URL`)
and disables local token issuing.

## Use it

Get a dev token for a seeded user (`admin@acme.test`, `editor@acme.test`,
`viewer@acme.test`, `admin@globex.test`). Tokens are minted where the database is:

```bash
TOKEN=$(docker compose exec -T tenant-mcp python -m app.harness.dev_token editor@acme.test)
```

```bash
curl -X POST http://localhost:8000/v1/chat -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"prompt\": \"Toggle the login page\", \"session_id\": \"demo\"}"
```

The response has `answer`, `actions[]`, `requires_confirmation` and `confirmation.token`.
To approve a gated action:

```bash
curl -X POST http://localhost:8000/v1/chat/confirm -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"token\": \"<token>\"}"
```

Interactive docs: http://localhost:8000/docs · CLI (asks before gated actions):
`docker compose exec app python run_workflow.py --token "$TOKEN"`

### External MCP clients

tenant-mcp accepts the same user JWTs directly (published on port 8001 in dev). For
example, with Claude Code:

```bash
claude mcp add --transport http tenant-tools http://localhost:8001/mcp --header "Authorization: Bearer $TOKEN"
```

External clients get the same tools, role checks and call logging (channel `external`).
Gated tools return a confirmation token that the user approves through
`POST /v1/chat/confirm` with their own JWT.

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
`llama3.1:8b`, `qwen3:14b` with more VRAM), `OLLAMA_FALLBACK_MODEL`, `JWT_SECRET` /
`JWT_JWKS_URL`, `REDIS_URL`, `RATE_LIMIT_*`, `DOCKER_DB_PASSWORD`, `LOG_LEVEL`,
`GRAPH_MODE` (off/advisory/enforce), `NEO4J_PASSWORD`, `RABBITMQ_PASSWORD`,
`RECONCILE_INTERVAL_SECONDS`.

## GPU

Requires Docker Desktop with WSL2 + an NVIDIA driver. Without a GPU, remove the
`deploy:` block from the `ollama` service (CPU inference is much slower).
