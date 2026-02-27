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
        default="google",
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
        default="claude-opus-4-5-20251101",
        description="Claude model for planning",
    )
    anthropic_model_executor: str = Field(
        default="claude-sonnet-4-20250514",
        description="Claude model for execution (faster for structured tasks)",
    )

    # Anthropic Skills (beta features)
    skills_enabled: bool = Field(
        default=True,
        description="Enable Anthropic Skills for document analysis",
    )
    skills_document_types: list = Field(
        default=["docx", "pdf", "xlsx", "pptx"],
        description="Document types to process with Skills",
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
        default="gemini-3-pro-preview",
        description="Gemini model for planning (main agent)",
    )
    google_model_executor: str = Field(
        default="gemini-3-flash-preview",
        description="Gemini model for execution (worker agent)",
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

    # Google Drive indexing
    drive_priority_folders: str = Field(
        default="dundee,myWayDigitalHealth,glucose.ai,birmingham",
        description="Comma-separated list of Drive folder names to fully index (text extraction + embeddings)",
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
    github_priority_repos: str = Field(
        default="",
        description="Comma-separated list of priority repos for deep indexing (e.g., owner/repo,owner/repo2)",
    )

    # Sync API (for remote session ingestion)
    sync_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="API key for remote session sync (cognitex-sync clients)",
    )

    # Web authentication
    web_allowed_emails: str = Field(
        default="",
        description="Comma-separated list of emails allowed to access web dashboard (invite-only)",
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

    # Autonomous Agent
    autonomous_agent_enabled: bool = Field(
        default=True,
        description="Enable the autonomous agent loop for proactive graph management",
    )
    autonomous_agent_interval_minutes: int = Field(
        default=15,
        ge=5,
        le=60,
        description="How often the autonomous agent runs (minutes)",
    )

    # Skill Evolution
    skill_evolution_enabled: bool = Field(
        default=True,
        description="Enable autonomous skill evolution (pattern detection + proposals)",
    )
    skill_evolution_cycle_interval: int = Field(
        default=10,
        ge=5,
        le=100,
        description="Run skill evolution every N autonomous agent cycles",
    )

    # Document exclusions for orphan detection
    # Documents matching these patterns won't be flagged as orphaned
    orphan_exclude_name_patterns: str = Field(
        default="mdt_,clinic_,IMG_",
        description="Comma-separated prefixes to exclude from orphan detection (e.g., 'mdt_,clinic_')",
    )
    orphan_exclude_mime_types: str = Field(
        default="application/vnd.google-makersuite",
        description="Comma-separated MIME type prefixes to exclude (e.g., AI Studio files)",
    )

    # Task creation mode
    task_creation_mode: Literal["auto", "propose"] = Field(
        default="propose",
        description="auto=create tasks immediately, propose=send for approval first",
    )

    # State-Aware Tool Filtering
    state_aware_tools_enabled: bool = Field(
        default=True,
        description="Filter available tools based on user's operating mode",
    )
    allow_tool_override: bool = Field(
        default=True,
        description="Allow users to temporarily override tool filtering",
    )

    # Context Summarization
    context_summarization_enabled: bool = Field(
        default=True,
        description="Auto-summarize older conversation history when context grows large",
    )
    max_context_tokens: int = Field(
        default=8000,
        ge=2000,
        le=32000,
        description="Token threshold before triggering summarization",
    )
    summarization_strategy: Literal["aggressive", "moderate", "minimal"] = Field(
        default="moderate",
        description="How aggressively to summarize (affects turns kept)",
    )
    recent_turns_to_keep: int = Field(
        default=7,
        ge=3,
        le=15,
        description="Number of recent conversation turns to keep verbatim",
    )

    # Clinical Data Firewall
    clinical_firewall_enabled: bool = Field(
        default=True,
        description="Enable pre-LLM clinical data filtering",
    )
    clinical_firewall_mode: Literal["block", "redact", "flag"] = Field(
        default="block",
        description="block=skip LLM entirely, redact=remove PHI then process, flag=process but warn",
    )
    clinical_firewall_patterns_path: str = Field(
        default="~/.cognitex/config/clinical_bypass_regex.txt",
        description="Path to clinical data regex patterns file",
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
