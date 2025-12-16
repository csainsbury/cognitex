"""Configuration management using pydantic-settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore extra env vars like POSTGRES_PASSWORD used by Docker
    )

    # Environment
    environment: Literal["development", "production", "testing"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # PostgreSQL
    database_url: str = Field(
        default="postgresql://cognitex:cognitex@localhost:5432/cognitex",
        description="PostgreSQL connection URL",
    )

    # Neo4j
    neo4j_uri: str = Field(default="bolt://localhost:7687")
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: SecretStr = Field(default=SecretStr("neo4j"))

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0")

    # Together.ai
    together_api_key: SecretStr = Field(default=SecretStr(""))
    together_model_planner: str = Field(
        default="deepseek-ai/DeepSeek-V3",
        description="Planner model for reasoning and decision making",
    )
    together_model_executor: str = Field(
        default="deepseek-ai/DeepSeek-V3",
        description="Executor model for structured tasks (email drafting, etc)",
    )
    together_model_embedding: str = Field(
        default="togethercomputer/m2-bert-80M-8k-retrieval",
        description="Model for generating embeddings",
    )
    # Legacy aliases for backward compatibility
    together_model_primary: str = Field(default="")
    together_model_fast: str = Field(default="")

    # Discord
    discord_bot_token: SecretStr = Field(default=SecretStr(""))
    discord_channel_id: str = Field(default="")

    # Google API
    google_client_id: str = Field(default="")
    google_client_secret: SecretStr = Field(default=SecretStr(""))
    google_credentials_path: str = Field(
        default="data/google_credentials.json",
        description="Path to store OAuth tokens",
    )

    # GitHub API
    github_token: SecretStr = Field(
        default=SecretStr(""),
        description="GitHub personal access token for repo access",
    )

    # Application behavior
    max_notifications_per_hour: int = Field(
        default=3,
        description="Throttle proactive Discord notifications",
    )
    default_energy_level: int = Field(
        default=7,
        ge=1,
        le=10,
        description="Default energy level if not specified",
    )

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
