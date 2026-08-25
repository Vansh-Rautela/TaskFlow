"""Settings loader.

Everything tunable lives in .env (secrets, paths) or config/*.yaml (behaviour).
Nothing tunable lives in Python. See ADR-004.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # app
    taskflow_env: str = "local"
    taskflow_tenant_id: str = "taskflow-demo"
    taskflow_api_token: str = "change-me"
    database_url: str = "sqlite+aiosqlite:///./data/taskflow.db"

    # retrieval
    qdrant_path: str = "./data/qdrant"
    qdrant_collection: str = "taskflow_kb"

    # llm
    taskflow_llm_mode: str = "cloud_first"  # cloud_first | local_only
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"

    # gmail & email
    gmail_credentials_path: str = "secrets/credentials.json"
    gmail_token_path: str = "secrets/token.json"
    gmail_label: str = "INBOX"
    gmail_user: str = ""
    gmail_app_password: str = ""
    gmail_poll_seconds: int = 15
    ops_email: str = ""

    # alerts
    teams_webhook_url: str = ""
    slack_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_webhook_url: str = ""


def _load_yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / name).read_text()) or {}


@lru_cache
def settings() -> Settings:
    return Settings()


@lru_cache
def providers_config() -> dict[str, Any]:
    return _load_yaml("providers.yaml")


@lru_cache
def thresholds_config() -> dict[str, Any]:
    return _load_yaml("thresholds.yaml")


@lru_cache
def policies_config() -> dict[str, Any]:
    return _load_yaml("policies.yaml")


@lru_cache
def app_config() -> dict[str, Any]:
    return _load_yaml("settings.yaml")


def model_for(provider: str, purpose: str) -> str:
    """The ONLY way to obtain a model name. Never hardcode one in source."""
    return str(providers_config()["providers"][provider]["models"][purpose])


def provider_priority() -> list[str]:
    cfg = providers_config()
    return list(cfg["priority"][settings().taskflow_llm_mode])
