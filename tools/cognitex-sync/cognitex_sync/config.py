"""Configuration management for cognitex-sync."""

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SyncConfig(BaseModel):
    """Sync configuration stored in config file."""

    server_url: str = ""
    api_key: str = ""
    machine_id: str = ""
    cli_paths: dict[str, str] = {}  # CLI type -> path mapping
    auto_summarize: bool = True  # Use local LLM to pre-summarize before sending
    sync_interval_minutes: int = 30


class Settings(BaseSettings):
    """Environment-based settings."""

    model_config = SettingsConfigDict(
        env_prefix="COGNITEX_SYNC_",
        env_file=".env",
        extra="ignore",
    )

    server_url: str = ""
    api_key: SecretStr = SecretStr("")
    machine_id: str = ""


def get_config_path() -> Path:
    """Get the config file path."""
    config_dir = Path.home() / ".config" / "cognitex-sync"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "config.json"


def get_state_path() -> Path:
    """Get the state file path (tracks synced sessions)."""
    config_dir = Path.home() / ".config" / "cognitex-sync"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "state.json"


def load_config() -> SyncConfig:
    """Load configuration from file and environment."""
    config_path = get_config_path()

    # Start with defaults
    config = SyncConfig()

    # Load from file if exists
    if config_path.exists():
        try:
            with open(config_path) as f:
                data = json.load(f)
                config = SyncConfig(**data)
        except Exception:
            pass

    # Override with environment variables
    settings = Settings()
    if settings.server_url:
        config.server_url = settings.server_url
    if settings.api_key.get_secret_value():
        config.api_key = settings.api_key.get_secret_value()
    if settings.machine_id:
        config.machine_id = settings.machine_id

    # Set default machine ID if not configured
    if not config.machine_id:
        import socket
        config.machine_id = socket.gethostname()

    # Set default CLI paths
    if not config.cli_paths:
        config.cli_paths = {
            "claude": str(Path.home() / ".claude" / "projects"),
        }

    return config


def save_config(config: SyncConfig) -> None:
    """Save configuration to file."""
    config_path = get_config_path()
    with open(config_path, "w") as f:
        json.dump(config.model_dump(), f, indent=2)


def load_state() -> dict:
    """Load sync state (tracks what's been synced)."""
    state_path = get_state_path()
    if state_path.exists():
        try:
            with open(state_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {"synced_sessions": {}, "last_sync": None}


def save_state(state: dict) -> None:
    """Save sync state."""
    state_path = get_state_path()
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
