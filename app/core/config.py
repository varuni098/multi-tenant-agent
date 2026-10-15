from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

DEV_JWT_SECRET = "dev-insecure-secret-change-me-0123456789"
DEV_MCP_CONTEXT_SECRET = "dev-insecure-mcp-context-secret-0123456789"
DEV_NEO4J_PASSWORD = "dev-graph-password"


class Settings(BaseSettings):
    """All runtime configuration. Values come from env vars (or .env), case-insensitive."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str | None = None  # full override, e.g. sqlite for tests
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "multi_tenant_agent_db"
    db_pool_size: int = 5

    # Local LLM (Ollama, OpenAI-compatible API)
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "qwen3:8b"
    ollama_keep_alive: str = "30m"
    warmup_on_startup: bool = True

    ollama_fallback_model: str = ""  # e.g. llama3.1:8b; empty = no fallback
    llm_timeout_seconds: float = 120
    max_requests_per_run: int = 8
    max_tool_calls_per_run: int = 10
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: int = 60

    app_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"

    # Auth: HS256 shared secret (dev/simple setups) or an OIDC provider's JWKS (RS256)
    jwt_secret: str = DEV_JWT_SECRET
    jwt_jwks_url: str | None = None
    jwt_issuer: str = "multi-tenant-agent"
    jwt_audience: str = "multi-tenant-agent"
    jwt_ttl_minutes: int = 60

    # State store (sessions, rate limits, confirmations); in-memory when unset
    redis_url: str | None = None

    # Harness limits
    max_prompt_chars: int = 2000
    rate_limit_user_per_minute: int = 20
    rate_limit_tenant_per_minute: int = 100
    session_ttl_seconds: int = 3600
    session_max_turns: int = 10
    confirmation_ttl_seconds: int = 300

    # tenant-mcp: the only service with database access. The agent app signs a short-lived
    # context token (tenant, user, role, purpose) with this shared secret on every call.
    tenant_mcp_url: str = "http://localhost:8001/mcp"
    mcp_context_secret: str = DEV_MCP_CONTEXT_SECRET
    mcp_context_ttl_seconds: int = 600

    # Knowledge graph (Phase 5). graph_mode: off = no graph tools or gate;
    # advisory = gate logs denials but lets writes through; enforce = gate blocks them.
    graph_mode: Literal["off", "advisory", "enforce"] = "off"
    graph_mcp_url: str = "http://localhost:8002/mcp"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = DEV_NEO4J_PASSWORD
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    relay_poll_seconds: float = 0.5
    reconcile_interval_seconds: int = 3600
    graph_change_events_per_tenant: int = 50

    # Knowledge / GraphRAG (Phase 6)
    embedding_provider: Literal["ollama", "hash"] = "ollama"  # hash = deterministic, for tests
    embedding_model: str = "nomic-embed-text"
    embedding_dimensions: int = 768
    docs_path: str = "docs"
    knowledge_model_concepts: bool = False  # extra concept pass with the chat model
    knowledge_watch_seconds: int = 0  # > 0: re-scan the docs folder at this interval
    chunk_target_chars: int = 1800
    chunk_overlap_chars: int = 200
    search_top_k: int = 4

    @property
    def graph_mcp_base_url(self) -> str:
        return self.graph_mcp_url.removesuffix("/").removesuffix("/mcp")

    @model_validator(mode="after")
    def _check_prod_secrets(self) -> "Settings":
        if self.app_env == "prod" and not self.jwt_jwks_url and self.jwt_secret == DEV_JWT_SECRET:
            raise ValueError("APP_ENV=prod requires JWT_SECRET (or JWT_JWKS_URL) to be set")
        if self.app_env == "prod" and self.mcp_context_secret == DEV_MCP_CONTEXT_SECRET:
            raise ValueError("APP_ENV=prod requires MCP_CONTEXT_SECRET to be set")
        if self.app_env == "prod" and self.graph_mode != "off" and self.neo4j_password == DEV_NEO4J_PASSWORD:
            raise ValueError("APP_ENV=prod with the graph enabled requires NEO4J_PASSWORD to be set")
        return self

    @property
    def tenant_mcp_base_url(self) -> str:
        """Server root for the internal (non-MCP) routes."""
        return self.tenant_mcp_url.removesuffix("/").removesuffix("/mcp")

    @property
    def sqlalchemy_url(self) -> str | URL:
        if self.database_url:
            return self.database_url
        return URL.create(
            "mysql+mysqlconnector",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    @property
    def ollama_native_url(self) -> str:
        """Ollama's native API root (without the OpenAI-compatible /v1 suffix)."""
        return self.ollama_base_url.removesuffix("/").removesuffix("/v1")


@lru_cache
def get_settings() -> Settings:
    return Settings()
