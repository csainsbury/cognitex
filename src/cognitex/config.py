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

    # LLM Provider Selection
    llm_provider: str = Field(
        default="together",
        description="Active LLM provider: together, anthropic, openai, or google",
    )

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
        default="BAAI/bge-base-en-v1.5",
        description="Model for generating embeddings (768 dimensions)",
    )
    # Legacy aliases for backward compatibility
    together_model_primary: str = Field(default="")
    together_model_fast: str = Field(default="")

    # Anthropic (Claude)
    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    anthropic_model_planner: str = Field(
        default="claude-sonnet-4-20250514",
        description="Claude model for planning",
    )
    anthropic_model_executor: str = Field(
        default="claude-sonnet-4-20250514",
        description="Claude model for execution",
    )

    # OpenAI
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    openai_model_planner: str = Field(
        default="gpt-4o",
        description="OpenAI model for planning",
    )
    openai_model_executor: str = Field(
        default="gpt-4o-mini",
        description="OpenAI model for execution",
    )
    openai_model_embedding: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model",
    )

    # Google (Gemini)
    google_ai_api_key: SecretStr = Field(default=SecretStr(""))
    google_model_planner: str = Field(
        default="gemini-2.0-flash",
        description="Gemini model for planning",
    )
    google_model_executor: str = Field(
        default="gemini-2.0-flash",
        description="Gemini model for execution",
    )

    # Discord
    discord_bot_token: SecretStr = Field(default=SecretStr(""))
    discord_channel_id: str = Field(default="")
    discord_user_id: str = Field(
        default="",
        description="Your Discord user ID for DM notifications on urgent messages",
    )

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
    github_auto_sync_repos: str = Field(
        default="csainsbury/cognitex,csainsbury/kre,csainsbury/ascend_v0.15,csainsbury/validact",
        description="Comma-separated list of repos to auto-sync daily (e.g., owner/repo,owner/repo2)",
    )

    # Push notifications
    webhook_base_url: str = Field(
        default="",
        description="Public HTTPS URL for webhooks (e.g., https://your-domain.com)",
    )
    google_pubsub_topic: str = Field(
        default="",
        description="Google Cloud Pub/Sub topic for Gmail push (projects/PROJECT/topics/TOPIC)",
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
