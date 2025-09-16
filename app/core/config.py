from functools import lru_cache

from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    PROJECT_NAME: str = "Enterprise log analyzer"
    API_PREFIX: str = "/api/v1"

    POSTGRES_SERVER: str = "postgres"
    POSTGRES_USER: str = "fastapi"
    POSTGRES_PASSWORD: str = "fastapi"
    POSTGRES_DB: str = "fastapi"

    DATABASE_URL: str | None = None
    TEST_DATABASE_URL: str | None = None
    REDIS_URL: str = "redis://localhost:6379/0"

    # Embeddings / Chroma configuration
    CHROMA_MODE: str = "local"  # "local" or "http"
    CHROMA_PERSIST_DIRECTORY: str = ".chroma"
    CHROMA_SERVER_HOST: str = "localhost"
    CHROMA_SERVER_PORT: int = 8000

    # Embedding provider and models
    EMBEDDING_PROVIDER: str = "openai"  # "openai" or "sentence-transformers"
    EMBEDDING_MODEL_NAME: str = "sentence-transformers/all-MiniLM-L6-v2"  # used when provider=sentence-transformers
    OPENAI_API_KEY: str | None = None
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    CHROMA_COLLECTION_PREFIX: str = "templates_"  # results: templates_macos, templates_linux, templates_windows

    # Redis stream config used by producer/consumer
    REDIS_URL: str = "redis://redis:6379/0"

    # Collections and streams
    CHROMA_LOG_COLLECTION_PREFIX: str = "logs_"
    CHROMA_PROTO_COLLECTION_PREFIX: str = "proto_"
    ALERTS_CANDIDATES_STREAM: str = "alerts_candidates"
    ALERTS_STREAM: str = "alerts"

    # Routing / clustering params
    NEAREST_PROTO_THRESHOLD: float = 0.25  # cosine distance threshold
    CLUSTER_MIN_SIZE: int = 5
    CLUSTER_DISTANCE_THRESHOLD: float = 0.3  # max cosine distance intra-cluster

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def sqlalchemy_database_uri(self) -> str:
        """Build the SQLAlchemy database URI."""
        if self.DATABASE_URL is not None:
            return str(self.DATABASE_URL)
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_SERVER}/{self.POSTGRES_DB}"
        )


@lru_cache()
def get_settings() -> Settings:  # pragma: no cover
    """Return cached settings object to avoid re-parsing env vars."""
    return Settings()

# Export a module-level settings instance for easy imports
settings = get_settings()
