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
  graph_mcp/  graph-mcp: read-only graph tools, search_docs, the can_apply write gate
  knowledge/  docs ingestion (loaders, chunker, embeddings, concepts), GraphRAG retrieval, eval
  live/       live updates: RabbitMQ -> Redis bridge, per-tenant pub/sub bus
  agent/      Pydantic AI agent + tools, model warm-up
  workflow/   LangGraph request flow (emits progress events for streaming)
  api/        FastAPI app: API v1 (chat, streaming, sessions, settings, audit, events)
ui/           Reflex web UI (chat with tool trace, settings browser, audit log)
ontology/     ontology.yaml (classes, relationships, cardinality, rules, aliases, concepts)
docs/         product/ (shared) and tenants/<tenant_id>/ (private) documentation
evals/        agent.yaml (golden dataset), retrieval.yaml, results/ (eval baselines)
migrations/   Alembic
tests/        unit/ (incl. the isolation suite), integration/, evals/ (agent eval runner), load/ (Locust)
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
ui (Reflex) --JWT--> app (harness + agent) --MCP--> tenant-mcp --> MySQL --outbox--> outbox-relay --> RabbitMQ
                         |   |   \--MCP + can_apply gate--> graph-mcp --> Neo4j <-- graph-sync <-------/ |
                         |   \--> Ollama (qwen3:8b)                       (re-projects, reconciles)       |
                         \--> Redis <-- live-bridge <-----------------------------------------------------/
                          (sessions, limits, live pub/sub)   external MCP clients (user JWT) -> tenant-mcp / graph-mcp
```

- **tenant-mcp** (`app/mcp_server/`) is the only service with database credentials, and
  the only one on the `data` network with MySQL. It serves the 12 tools over MCP
  (streamable HTTP) and internal routes for the app: `/internal/whoami`,
  `/internal/confirm`, and the read-only views `/internal/settings` and `/internal/audit`.
- **The agent app** has no database access. Every call to tenant-mcp carries a signed
  `X-Tenant-Context` (tenant, user, role, purpose, request ID; HS256 with
  `MCP_CONTEXT_SECRET`). The server verifies it and re-checks membership, tenant status
  and role in the database.
- **Purposes** bind credentials to endpoints. `auth` is for whoami, `agent` for MCP
  tools, `confirm` for executing a confirmation, and `api` for the REST views. An agent
  run can never approve its own gated action, and `api` credentials can't call tools.
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
| `search_docs` | viewer+ | documentation search with citations (see above) |
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

## Documentation and GraphRAG

`docs/product/` is shared product documentation. `docs/tenants/<tenant_id>/` is visible only
to that tenant. Markdown, HTML, PDF and text are supported.

**Ingestion** (`knowledge` service; re-scans every `KNOWLEDGE_WATCH_SECONDS`, default 30):

1. Load and normalise each document. HTML headings are kept, and PDFs are split per page.
2. Chunk by heading into sections of about 500 tokens (1,800 characters, 200 overlap).
3. Embed with `nomic-embed-text` on Ollama (768 dimensions, with search prefixes) and store
   as `DocChunk` nodes behind a Neo4j vector index.
4. Link concepts: a whole-word match against the ontology vocabulary, plus an optional
   constrained model pass (`KNOWLEDGE_MODEL_CONCEPTS`). Only known concepts are kept.
   `DocChunk -MENTIONS-> Concept -ABOUT-> SettingKey | Feature | Template | Plan | Role`.
5. Track freshness with content hashes. Unchanged documents are skipped, edited ones are
   replaced atomically, and deleted ones are removed.

**Retrieval** (`search_docs`, viewer+, on graph-mcp):

1. Take the top vector matches for this tenant plus the shared docs.
2. Add chunks that mention the query's concepts (one hop in the graph).
3. Re-rank by `0.75 * similarity + 0.25 * concept overlap`.
4. Return excerpts with citations ("[1] Templates › Switching templates").

The agent answers only from the excerpts and cites them. The API response lists them in
`citations[]`.

**Eval** (`evals/retrieval.yaml`, 21 cases including tenant-leak checks):

```bash
docker compose exec graph-mcp python -m app.knowledge.evaluate
```

Live with `nomic-embed-text`: recall@4 = 1.00, MRR = 0.97, 0 leaks. The thresholds are
0.85 / 0.70 / 0, and the command exits 1 below them.

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
| `app`         | FastAPI (API v1)                             | 8000          |
| `ui`          | Reflex web UI                                | 3000 (+3001 dev) |
| `live-bridge` | RabbitMQ -> Redis live channel               | –             |
| `ollama`      | Local LLM server (GPU)                       | 11435         |
| `ollama-pull` | One-shot: pulls `$OLLAMA_MODEL`              | –             |
| `mysql`       | MySQL 8                                      | 3308          |
| `tenant-mcp`  | MCP tool server; only service with DB access | 8001          |
| `graph-mcp`   | Graph tools + can_apply gate (Neo4j only)    | 8002          |
| `neo4j`       | Knowledge graph                              | 7475 / 7688   |
| `rabbitmq`    | Event broker (outbox -> graph-sync, live-bridge) | 15673 (UI) |
| `outbox-relay`| MySQL outbox -> RabbitMQ                     | –             |
| `graph-sync`  | RabbitMQ -> Neo4j projection, reconciler     | –             |
| `knowledge`   | Docs ingestion (embeddings, concepts)        | –             |
| `redis`       | Sessions, rate limits, confirmations, live pub/sub | –       |
| `migrate`     | One-shot: `alembic upgrade head` (+ seed in dev) | –         |

On startup the app loads the model onto the GPU in the background, so the first
request isn't slow.

## API v1

Interactive docs at http://localhost:8000/docs (OpenAPI at `/openapi.json`).

| Endpoint | Who | What |
|----------|-----|------|
| `POST /v1/chat` | any member | one turn, answered when complete |
| `POST /v1/chat/stream` | any member | the same turn as Server-Sent Events (below) |
| `POST /v1/chat/confirm` | the user who asked | carry out a pending destructive action |
| `GET` / `DELETE /v1/sessions/{id}` | the session's owner | the caller's own turns; other users get 404 |
| `GET /v1/me` | any member | tenant, email, effective role, pages |
| `GET /v1/settings` | any member | effective settings of every page plus tenant defaults, with sources |
| `GET /v1/audit` | admin | `kind=changes` (setting changes) or `kind=tool_calls`, newest first; page with `before_id` = `next_before_id`; filter by `actor`, `page_name` |
| `GET /v1/events` | any member | live tenant changes as Server-Sent Events (below) |
| `GET /health`, `GET /ready` | public | liveness; readiness of Ollama, tenant-mcp and Redis (`graph` is reported but optional: writes fail closed without it) |

**Errors** always have one shape, with the HTTP status mapped to a code (`unauthorized`,
`forbidden`, `not_found`, `conflict`, `invalid_request`, `rate_limited`, `unavailable`):

```json
{"error": {"code": "forbidden", "message": "Only admins can read the audit log.", "request_id": "..."}}
```

**Versioning.** Everything under `/v1` changes only additively (new endpoints, optional
fields, event types). A breaking change ships as `/v2` alongside `/v1`, and `/v1`
responses then carry a `Deprecation` header for at least one release before removal.
Clients should ignore unknown fields and event types.

**Streaming chat** (`/v1/chat/stream`) sends `start`, then `intent` and `thinking`, then
`tool_start` / `tool_end` for each tool call (outcome `ok`, `changed`, `denied`,
`needs_confirmation` or `error`), then `result` (the same body as `/v1/chat`) and
`done`. The answer arrives whole in `result`, because the output validator checks each
draft against what actually happened and may reject it. Streaming draft tokens would
show text that might be retracted. Confirmation tokens appear only in `result`. If the
client disconnects, the turn still finishes, so nothing is left half-applied.

```bash
curl -N -X POST http://localhost:8000/v1/chat/stream -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"prompt\": \"Make the dashboard dark\"}"
```

**Live updates** (`/v1/events`). Every committed change goes MySQL outbox -> RabbitMQ ->
`live-bridge` -> Redis pub/sub (`live:<tenant_id>`) -> the API stream. It doesn't matter
who made the change (agent, MCP client or another user). Events are `setting.changed`,
`page.created`, `page.deleted` and `tenant.template_changed`, with `{page_name, key,
after, actor, at}`. Request IDs and other payload fields stay server-side. The bridge
uses an exclusive, non-durable queue, so nothing piles up while nobody is watching.
Streams end after 15 minutes; reconnect (re-authenticating) to continue.

## Web UI

http://localhost:3000, served by Reflex (`ui/`).

- **Sign in.** In dev, pick a seeded user. Otherwise paste a bearer token. The token
  lives only in the UI backend (a backend-only state var) and never reaches the browser.
- **Chat.** Each tool call appears live as it runs (`set_theme(page_name=login,
  theme=dark) → changed`), and answers show their documentation sources. Destructive
  actions open a confirmation dialog.
- **Settings.** Every page's effective values and where they come from (page, template,
  default), with a preview card per page. The UI itself wears the theme of the page you
  pick ("UI follows", default `dashboard`). When anyone changes that page, the UI
  switches theme instantly and shows a toast.
- **Audit** (admins). Setting changes and tool calls, with paging.

The UI talks only to the agent API (`AGENT_API_URL`), never to MCP servers or
databases. In dev, Vite (3000) and the Reflex backend (3001) hot-reload on edits. In
production, `reflex run --env prod --single-port` serves both on 3000, and dev login is
off. Reflex's `bun` comes from the official image at build time, so nothing is
downloaded when the container starts.

## Development (auto-reload)

```bash
cp .env.example .env
docker compose up --build --watch
```

`compose.override.yml` is loaded automatically: code edits sync into the
container and uvicorn reloads; changes to requirements or the Dockerfile rebuild.

Optional git hooks: `pip install pre-commit && pre-commit install`. See
[Testing and evals](#testing-and-evals) for the checks.

## Testing and evals

| Layer | Command | Needs | What it proves |
|-------|---------|-------|----------------|
| Lint, types, unit | `docker compose run --rm --no-deps app sh -c "ruff check . && ruff format --check . && mypy && pytest --cov"` | nothing (SQLite, scripted model) | behaviour of every module; coverage floor 75% |
| Isolation (merge-blocking) | `docker compose run --rm --no-deps app pytest -m isolation` | nothing | tenants, roles and credentials can't be crossed |
| Integration | `docker compose --profile test run --rm tests` | throwaway Neo4j + RabbitMQ | graph sync, can_apply, GraphRAG; unit + integration coverage 89% |
| Agent eval | `docker compose --profile eval run --rm evals` | Ollama (GPU) | the real model picks the right tools and answers faithfully |
| Retrieval eval | `docker compose exec graph-mcp python -m app.knowledge.evaluate` | stack running | documentation search finds the right sections |
| Load | `docker compose --profile load run --rm locust` | stack running | throughput and latency under concurrency |

**Isolation suite** (`pytest -m isolation`, 108 tests) runs inside the unit suite, so it
gates every merge:

- **Cross-tenant tools:** each of the 12 tenant tools is called by Globex's admin, aimed
  at Acme's pages. Acme's data must be byte-for-byte unchanged and never appear in the
  output, including after confirming any token that comes back.
- **Role escalation:** a 12 tools × 3 database roles matrix, with tokens that claim
  admin. Access follows the database role exactly.
- **Credential purposes:** every purpose (`auth`, `agent`, `confirm`, `api`) against
  every internal route and `/mcp`. User tokens never reach internal routes, and
  non-members are rejected on every channel.
- **Prompt injection:** 15 attacks run with a model scripted to comply and try
  everything. The other tenant stays untouched and unmentioned.
- **Talk-your-way-in:** viewers attempting writes get no write tools and no effect.
- **Session memory:** stays per tenant.

**Agent eval** (`evals/agent.yaml`, 147 cases) covers reads, writes, multi-step and
follow-up turns, fuzzy page names, gated actions, permissions, plan entitlements, invalid
values, off-topic and injection. Each case runs the full harness against the real model on
freshly seeded SQLite data, so your databases are never touched, and is scored with
pydantic-evals:

| Metric | Meaning | Threshold |
|--------|---------|-----------|
| tool selection | expected tools (or an accepted alternative) called, forbidden ones not | ≥ 95% |
| argument accuracy | the calls carried the expected arguments | ≥ 90% |
| task success | data ends exactly as expected; confirmations pending when they should be | ≥ 90% |
| faithfulness | the answer states the expected facts and never claims a change that didn't happen | ≥ 90% |
| isolation | the other tenant untouched and absent from answers and tool arguments | 100% |
| latency p95 | one turn, end to end | ≤ 15 s |

Baseline (`evals/results/agent-qwen3_8b-*.json`, RTX 5050 8 GB):

- **Tool selection** 98.6%.
- **Arguments** 98.1%.
- **Task success** 98.0%.
- **Faithfulness** 98.6%.
- **Isolation** 100%.
- **Latency** p50 10.2 s, **p95 31.9 s (fails the target)**, with a mean of 2,682 tokens
  per turn.

The five misses are real model errors:

- "German" was passed instead of `de`.
- "hide tips" was not mapped to `show_tips`.
- A delete was confirmed in prose instead of with the gated tool.
- A "purple" theme request was mistaken for a brand colour.
- A template request got a setting error instead of a permission refusal.

To compare a model or prompt change:

```bash
docker compose exec ollama ollama pull llama3.1:8b
docker compose --profile eval run --rm evals --model llama3.1:8b
docker compose --profile eval run --rm evals --compare evals/results/<baseline>.json evals/results/<candidate>.json
```

`--only <text>` and `--limit N` run a subset. The command exits 1 when a threshold is missed.

**Load test** (Locust, read-only traffic, so dev data is never changed): 2 users for 3
minutes on qwen3:8b with an 8 GB GPU.

| `OLLAMA_NUM_PARALLEL` | model turns/min | p50 | p95 | VRAM |
|---|---|---|---|---|
| 1 | 5.3 | 17 s | 21 s | 5.6 GB |
| 2 (default) | 7.3 | 11 s | 20 s | 6.2 GB |

Overloading it showed abandoned turns piling up behind the GPU (one took 112 s), so the
API now admits at most `MAX_CONCURRENT_RUNS` turns per process (default 4). Others wait
up to `RUN_QUEUE_SECONDS` (default 5), then get `503` with `Retry-After`. Use
`--service-ports` to open Locust's web UI at http://localhost:8089.

## Production

```bash
docker compose -f compose.yml -f compose.prod.yml up -d --build
```

No reload, 2 uvicorn workers, restart policies and resource limits; only ports
8000 (API) and 3000 (UI) are published. `APP_ENV=prod` requires a real `JWT_SECRET` (or `JWT_JWKS_URL`)
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
`RECONCILE_INTERVAL_SECONDS`, `EMBEDDING_MODEL` (default `nomic-embed-text`), `KNOWLEDGE_MODEL_CONCEPTS`,
`KNOWLEDGE_WATCH_SECONDS`, `OLLAMA_NUM_PARALLEL` (default 2), `MAX_CONCURRENT_RUNS`,
`RUN_QUEUE_SECONDS`, `UI_PUBLIC_API_URL` (the address browsers use for the UI backend;
set it when the UI isn't on localhost).

## GPU

Requires Docker Desktop with WSL2 + an NVIDIA driver. Without a GPU, remove the
`deploy:` block from the `ollama` service (CPU inference is much slower).
