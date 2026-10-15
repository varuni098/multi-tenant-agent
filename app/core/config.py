from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


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

    # Used when a prompt contains no tenant UUID (temporary until auth lands)
    default_tenant_uuid: str = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"

    log_level: str = "INFO"

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
