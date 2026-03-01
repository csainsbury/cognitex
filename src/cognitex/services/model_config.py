"""Model configuration service with runtime switching via Redis.

Supports multiple LLM providers: Together.ai, Anthropic, OpenAI, and Google.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from typing import Any, Literal

import structlog

from cognitex.config import get_settings

logger = structlog.get_logger()

# Redis key for model configuration
REDIS_KEY = "cognitex:model_config"

# Provider type
ProviderType = Literal["together", "anthropic", "openai", "google", "openrouter"]

# Available providers with their display names
PROVIDERS = {
    "together": "Together.ai",
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI (GPT)",
    "google": "Google (Gemini)",
    "openrouter": "OpenRouter",
}

# Popular Together.ai models for different purposes
TOGETHER_CHAT_MODELS = [
    # Latest/Best
    {"id": "deepseek-ai/DeepSeek-V3", "display_name": "DeepSeek V3", "context_length": 128000},
    {"id": "deepseek-ai/DeepSeek-R1", "display_name": "DeepSeek R1 (Reasoning)", "context_length": 128000},
    {"id": "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8", "display_name": "Llama 4 Maverick 17B", "context_length": 131072},
    {"id": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "display_name": "Llama 3.3 70B Turbo", "context_length": 131072},
    {"id": "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo", "display_name": "Llama 3.1 405B Turbo", "context_length": 130815},
    {"id": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo", "display_name": "Llama 3.1 70B Turbo", "context_length": 131072},
    {"id": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo", "display_name": "Llama 3.1 8B Turbo (Fast)", "context_length": 131072},
    {"id": "Qwen/Qwen3-30B-A3B", "display_name": "Qwen3 30B MoE", "context_length": 40960},
    {"id": "Qwen/Qwen2.5-72B-Instruct-Turbo", "display_name": "Qwen 2.5 72B Turbo", "context_length": 32768},
    {"id": "Qwen/QwQ-32B", "display_name": "QwQ 32B (Reasoning)", "context_length": 131072},
    {"id": "mistralai/Mistral-Small-24B-Instruct-2501", "display_name": "Mistral Small 24B", "context_length": 32768},
]

TOGETHER_EMBEDDING_MODELS = [
    {"id": "BAAI/bge-base-en-v1.5", "display_name": "BGE Base (768d)", "dimensions": 768},
    {"id": "BAAI/bge-large-en-v1.5", "display_name": "BGE Large (1024d)", "dimensions": 1024},
    {"id": "togethercomputer/m2-bert-80M-8k-retrieval", "display_name": "M2-BERT 8k", "dimensions": 768},
    {"id": "togethercomputer/m2-bert-80M-32k-retrieval", "display_name": "M2-BERT 32k", "dimensions": 768},
]

# Anthropic Claude models
ANTHROPIC_CHAT_MODELS = [
    {"id": "claude-sonnet-4-20250514", "display_name": "Claude Sonnet 4 (Latest)", "context_length": 200000},
    {"id": "claude-opus-4-20250514", "display_name": "Claude Opus 4 (Most Capable)", "context_length": 200000},
    {"id": "claude-3-5-sonnet-20241022", "display_name": "Claude 3.5 Sonnet", "context_length": 200000},
    {"id": "claude-3-5-haiku-20241022", "display_name": "Claude 3.5 Haiku (Fast)", "context_length": 200000},
    {"id": "claude-3-opus-20240229", "display_name": "Claude 3 Opus", "context_length": 200000},
]

# OpenAI models
OPENAI_CHAT_MODELS = [
    {"id": "gpt-4o", "display_name": "GPT-4o (Latest)", "context_length": 128000},
    {"id": "gpt-4o-mini", "display_name": "GPT-4o Mini (Fast/Cheap)", "context_length": 128000},
    {"id": "gpt-4-turbo", "display_name": "GPT-4 Turbo", "context_length": 128000},
    {"id": "o1", "display_name": "o1 (Reasoning)", "context_length": 200000},
    {"id": "o1-mini", "display_name": "o1 Mini (Reasoning/Fast)", "context_length": 128000},
    {"id": "o3-mini", "display_name": "o3 Mini (Advanced Reasoning)", "context_length": 200000},
]

OPENAI_EMBEDDING_MODELS = [
    {"id": "text-embedding-3-small", "display_name": "Embedding 3 Small (1536d)", "dimensions": 1536},
    {"id": "text-embedding-3-large", "display_name": "Embedding 3 Large (3072d)", "dimensions": 3072},
    {"id": "text-embedding-ada-002", "display_name": "Ada 002 (Legacy)", "dimensions": 1536},
]

# Google Gemini models
GOOGLE_CHAT_MODELS = [
    {"id": "gemini-3.1-pro-preview", "display_name": "Gemini 3.1 Pro (Latest)", "context_length": 1000000},
    {"id": "gemini-3-flash-preview", "display_name": "Gemini 3 Flash (Fast)", "context_length": 1000000},
    {"id": "gemini-3-pro-preview", "display_name": "Gemini 3 Pro (Deprecated)", "context_length": 1000000},
    {"id": "gemini-2.0-flash", "display_name": "Gemini 2.0 Flash", "context_length": 1000000},
    {"id": "gemini-2.0-flash-thinking-exp", "display_name": "Gemini 2.0 Flash Thinking", "context_length": 1000000},
    {"id": "gemini-1.5-pro", "display_name": "Gemini 1.5 Pro (2M ctx)", "context_length": 2000000},
    {"id": "gemini-1.5-flash", "display_name": "Gemini 1.5 Flash", "context_length": 1000000},
]

# OpenRouter models (multi-provider gateway, OpenAI-compatible)
OPENROUTER_CHAT_MODELS = [
    # Anthropic (via OpenRouter)
    {"id": "anthropic/claude-sonnet-4", "display_name": "Claude Sonnet 4", "context_length": 200000},
    {"id": "anthropic/claude-opus-4", "display_name": "Claude Opus 4", "context_length": 200000},
    {"id": "anthropic/claude-haiku-3.5", "display_name": "Claude 3.5 Haiku (Fast)", "context_length": 200000},
    # Google
    {"id": "google/gemini-2.5-pro-preview", "display_name": "Gemini 2.5 Pro", "context_length": 1000000},
    {"id": "google/gemini-2.5-flash-preview", "display_name": "Gemini 2.5 Flash", "context_length": 1000000},
    # DeepSeek
    {"id": "deepseek/deepseek-r1", "display_name": "DeepSeek R1 (Reasoning)", "context_length": 128000},
    {"id": "deepseek/deepseek-chat-v3-0324", "display_name": "DeepSeek V3", "context_length": 128000},
    # Meta Llama
    {"id": "meta-llama/llama-4-maverick", "display_name": "Llama 4 Maverick", "context_length": 131072},
    {"id": "meta-llama/llama-3.3-70b-instruct", "display_name": "Llama 3.3 70B", "context_length": 131072},
    # Qwen
    {"id": "qwen/qwen3-235b-a22b", "display_name": "Qwen3 235B", "context_length": 40960},
    {"id": "qwen/qwq-32b", "display_name": "QwQ 32B (Reasoning)", "context_length": 131072},
    # Mistral
    {"id": "mistralai/mistral-medium-3", "display_name": "Mistral Medium 3", "context_length": 131072},
    # xAI
    {"id": "x-ai/grok-3-mini-beta", "display_name": "Grok 3 Mini", "context_length": 131072},
]

# Backwards compatibility
RECOMMENDED_CHAT_MODELS = [m["id"] for m in TOGETHER_CHAT_MODELS]
RECOMMENDED_EMBEDDING_MODELS = [m["id"] for m in TOGETHER_EMBEDDING_MODELS]

# Short aliases for quick model switching via slash commands
MODEL_ALIASES: dict[str, tuple[str, str]] = {
    # (provider, model_id)
    "sonnet": ("anthropic", "claude-sonnet-4-20250514"),
    "opus": ("anthropic", "claude-opus-4-20250514"),
    "haiku": ("anthropic", "claude-3-5-haiku-20241022"),
    "sonnet-3.5": ("anthropic", "claude-3-5-sonnet-20241022"),
    "gpt4o": ("openai", "gpt-4o"),
    "gpt4o-mini": ("openai", "gpt-4o-mini"),
    "o1": ("openai", "o1"),
    "o3-mini": ("openai", "o3-mini"),
    "gemini-pro": ("google", "gemini-3.1-pro-preview"),
    "gemini-flash": ("google", "gemini-3-flash-preview"),
    "deepseek": ("together", "deepseek-ai/DeepSeek-V3"),
    "deepseek-r1": ("together", "deepseek-ai/DeepSeek-R1"),
    "llama-70b": ("together", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "llama-405b": ("together", "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo"),
    "qwen": ("together", "Qwen/Qwen3-30B-A3B"),
    # OpenRouter aliases
    "grok": ("openrouter", "x-ai/grok-3-mini-beta"),
    "maverick": ("openrouter", "meta-llama/llama-4-maverick"),
    "r1": ("openrouter", "deepseek/deepseek-r1"),
    "mistral": ("openrouter", "mistralai/mistral-medium-3"),
}

# Per-task model override slots
TASK_MODEL_SLOTS = (
    "autonomous",
    "triage",
    "draft",
    "context_pack",
    "skill_evolution",
)


@dataclass
class ModelConfig:
    """Current model configuration with provider support."""

    provider: str  # together, anthropic, openai, google
    planner_model: str
    executor_model: str
    embedding_model: str
    embedding_provider: str = "together"  # Embedding provider (may differ from chat)
    # Per-task model overrides (empty string = use planner_model)
    autonomous_model: str = ""
    triage_model: str = ""
    draft_model: str = ""
    context_pack_model: str = ""
    skill_evolution_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelConfig":
        return cls(
            provider=data.get("provider", "together"),
            planner_model=data.get("planner_model", "deepseek-ai/DeepSeek-V3"),
            executor_model=data.get("executor_model", "deepseek-ai/DeepSeek-V3"),
            embedding_model=data.get("embedding_model", "BAAI/bge-base-en-v1.5"),
            embedding_provider=data.get("embedding_provider", "together"),
            autonomous_model=data.get("autonomous_model", ""),
            triage_model=data.get("triage_model", ""),
            draft_model=data.get("draft_model", ""),
            context_pack_model=data.get("context_pack_model", ""),
            skill_evolution_model=data.get("skill_evolution_model", ""),
        )

    def get_model_for_task(self, task: str) -> str:
        """Return the override model for a task slot, or planner_model if unset."""
        field_name = f"{task}_model"
        override = getattr(self, field_name, "")
        return override if override else self.planner_model

    @classmethod
    def from_settings(cls) -> "ModelConfig":
        """Create config from environment settings."""
        settings = get_settings()
        provider = settings.llm_provider

        # Get models based on provider
        if provider == "anthropic":
            planner = settings.anthropic_model_planner
            executor = settings.anthropic_model_executor
            embedding = settings.together_model_embedding  # Anthropic has no embeddings
            embedding_provider = "together"
        elif provider == "openai":
            planner = settings.openai_model_planner
            executor = settings.openai_model_executor
            embedding = settings.openai_model_embedding
            embedding_provider = "openai"
        elif provider == "google":
            planner = settings.google_model_planner
            executor = settings.google_model_executor
            embedding = settings.together_model_embedding  # Use Together for embeddings
            embedding_provider = "together"
        elif provider == "openrouter":
            planner = settings.openrouter_model_planner
            executor = settings.openrouter_model_executor
            embedding = settings.together_model_embedding  # Use Together for embeddings
            embedding_provider = "together"
        else:  # together (default)
            planner = settings.together_model_planner
            executor = settings.together_model_executor
            embedding = settings.together_model_embedding
            embedding_provider = "together"

        return cls(
            provider=provider,
            planner_model=planner,
            executor_model=executor,
            embedding_model=embedding,
            embedding_provider=embedding_provider,
        )


class ModelConfigService:
    """Service for managing model configuration at runtime."""

    def __init__(self):
        self._redis = None
        self._together_client = None
        self._cached_models: dict[str, list[dict]] = {}  # Cache per provider

    async def _get_redis(self):
        """Get Redis connection."""
        if self._redis is None:
            import redis.asyncio as redis
            settings = get_settings()
            self._redis = redis.from_url(settings.redis_url)
        return self._redis

    def _get_together_client(self):
        """Get Together.ai client."""
        if self._together_client is None:
            from together import Together
            settings = get_settings()
            api_key = settings.together_api_key.get_secret_value()
            if api_key:
                self._together_client = Together(api_key=api_key)
        return self._together_client

    def get_chat_models_for_provider(self, provider: str) -> list[dict]:
        """Get recommended chat models for a specific provider."""
        if provider == "together":
            return TOGETHER_CHAT_MODELS
        elif provider == "anthropic":
            return ANTHROPIC_CHAT_MODELS
        elif provider == "openai":
            return OPENAI_CHAT_MODELS
        elif provider == "google":
            return GOOGLE_CHAT_MODELS
        elif provider == "openrouter":
            return OPENROUTER_CHAT_MODELS
        return TOGETHER_CHAT_MODELS

    def get_embedding_models_for_provider(self, provider: str) -> list[dict]:
        """Get embedding models for a specific provider."""
        if provider == "together":
            return TOGETHER_EMBEDDING_MODELS
        elif provider == "openai":
            return OPENAI_EMBEDDING_MODELS
        # Anthropic, Google, and OpenRouter don't have their own embeddings, use Together
        return TOGETHER_EMBEDDING_MODELS

    def get_available_providers(self) -> list[dict]:
        """Get list of available providers with their API key status."""
        settings = get_settings()
        providers = []
        for key, name in PROVIDERS.items():
            has_key = False
            if key == "together":
                has_key = bool(settings.together_api_key.get_secret_value())
            elif key == "anthropic":
                has_key = bool(settings.anthropic_api_key.get_secret_value())
            elif key == "openai":
                has_key = bool(settings.openai_api_key.get_secret_value())
            elif key == "google":
                has_key = bool(settings.google_ai_api_key.get_secret_value())
            elif key == "openrouter":
                has_key = bool(settings.openrouter_api_key.get_secret_value())

            providers.append({
                "id": key,
                "name": name,
                "has_api_key": has_key,
            })
        return providers

    async def get_config(self) -> ModelConfig:
        """Get current model configuration from Redis, falling back to settings."""
        try:
            redis = await self._get_redis()
            data = await redis.get(REDIS_KEY)
            if data:
                return ModelConfig.from_dict(json.loads(data))
        except Exception as e:
            logger.warning("Failed to get model config from Redis", error=str(e))

        # Fall back to settings
        return ModelConfig.from_settings()

    async def set_config(self, config: ModelConfig) -> None:
        """Save model configuration to Redis."""
        try:
            redis = await self._get_redis()
            await redis.set(REDIS_KEY, json.dumps(config.to_dict()))
            logger.info(
                "Updated model configuration",
                planner=config.planner_model,
                executor=config.executor_model,
                embedding=config.embedding_model,
            )
        except Exception as e:
            logger.error("Failed to save model config to Redis", error=str(e))
            raise

    async def update_task_model(self, task: str, model_id: str) -> ModelConfig:
        """Update a per-task model override and return new config."""
        if task not in TASK_MODEL_SLOTS:
            raise ValueError(f"Unknown task slot: {task}")
        config = await self.get_config()
        setattr(config, f"{task}_model", model_id)
        await self.set_config(config)
        return config

    async def update_model(
        self,
        model_type: str,
        model_id: str,
    ) -> ModelConfig:
        """Update a specific model and return new config."""
        config = await self.get_config()

        if model_type == "planner":
            config.planner_model = model_id
        elif model_type == "executor":
            config.executor_model = model_id
        elif model_type == "embedding":
            config.embedding_model = model_id
        else:
            raise ValueError(f"Unknown model type: {model_type}")

        await self.set_config(config)
        return config

    async def list_available_models(self, refresh: bool = False) -> list[dict]:
        """
        List available models from Together.ai.

        Returns cached list unless refresh=True.
        """
        if self._cached_models and not refresh:
            return self._cached_models

        try:
            client = self._get_together_client()
            response = client.models.list()

            models = []
            for model in response:
                model_dict = {
                    "id": model.id,
                    "display_name": getattr(model, "display_name", model.id),
                    "type": getattr(model, "type", "unknown"),
                    "context_length": getattr(model, "context_length", None),
                }
                models.append(model_dict)

            # Sort by display name
            models.sort(key=lambda m: m["display_name"].lower())
            self._cached_models = models

            logger.info("Fetched models from Together.ai", count=len(models))
            return models

        except Exception as e:
            logger.error("Failed to fetch models from Together.ai", error=str(e))
            # Return recommended models as fallback
            return [
                {"id": m, "display_name": m, "type": "chat"}
                for m in RECOMMENDED_CHAT_MODELS
            ]

    async def list_chat_models(self, refresh: bool = False) -> list[dict]:
        """List available chat/language models."""
        all_models = await self.list_available_models(refresh)
        chat_models = [
            m for m in all_models
            if m.get("type") in ("chat", "language", "code", None)
            and not m["id"].startswith("BAAI/")
            and not "embed" in m["id"].lower()
            and not "rerank" in m["id"].lower()
        ]

        # Ensure recommended models are at the top
        recommended_set = set(RECOMMENDED_CHAT_MODELS)
        recommended = [m for m in chat_models if m["id"] in recommended_set]
        others = [m for m in chat_models if m["id"] not in recommended_set]

        # Sort recommended by their order in the list
        recommended.sort(key=lambda m: RECOMMENDED_CHAT_MODELS.index(m["id"]) if m["id"] in RECOMMENDED_CHAT_MODELS else 999)

        return recommended + others

    async def list_embedding_models(self, refresh: bool = False) -> list[dict]:
        """List available embedding models."""
        all_models = await self.list_available_models(refresh)
        embedding_models = [
            m for m in all_models
            if m.get("type") in ("embedding",)
            or "embed" in m["id"].lower()
            or m["id"].startswith("BAAI/")
        ]

        # Ensure recommended models are at the top
        recommended_set = set(RECOMMENDED_EMBEDDING_MODELS)
        recommended = [m for m in embedding_models if m["id"] in recommended_set]
        others = [m for m in embedding_models if m["id"] not in recommended_set]

        return recommended + others


# Singleton
_model_config_service: ModelConfigService | None = None


def get_model_config_service() -> ModelConfigService:
    """Get the model config service singleton."""
    global _model_config_service
    if _model_config_service is None:
        _model_config_service = ModelConfigService()
    return _model_config_service


async def get_active_models() -> tuple[str, str, str]:
    """
    Get the currently active models.

    Returns:
        Tuple of (planner_model, executor_model, embedding_model)
    """
    service = get_model_config_service()
    config = await service.get_config()
    return config.planner_model, config.executor_model, config.embedding_model
