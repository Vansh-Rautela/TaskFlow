"""Settings loader.

Everything tunable lives in .env (secrets, paths) or config/*.yaml (behaviour).
Nothing tunable lives in Python. See ADR-004.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

from taskflow.domain.errors import ConfigError

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # app
    taskflow_env: str = "local"
    taskflow_tenant_id: str = "taskflow-demo"
    taskflow_api_token: str = "change-me"
    taskflow_api_base_url: str = "http://localhost:8000"
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
    gmail_inbound_mode: str = "poll"  # poll (IMAP) | push (Gmail API watch + Pub/Sub)
    gmail_credentials_path: str = "secrets/credentials.json"
    gmail_token_path: str = "secrets/token.json"
    gmail_label: str = "INBOX"
    gmail_user: str = ""
    gmail_app_password: str = ""
    gmail_poll_seconds: int = 15
    ops_email: str = ""

    # gmail push (docs/14_GMAIL_PUSH_SETUP.md) — unused until gmail_inbound_mode=push
    gcp_project_id: str = ""
    pubsub_topic: str = ""
    pubsub_subscription: str = ""

    # alerts
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


def retrieval_config() -> dict[str, Any]:
    """The `retrieval` block from settings.yaml — sizing knobs and model names.

    Decision boundaries (`min_top_score` and friends) live in thresholds.yaml and are
    read through `thresholds_config`. Keeping the two apart is what stops a knob and
    a threshold from drifting into two copies of the same value.
    """
    return dict(app_config().get("retrieval", {}))


def pii_config() -> dict[str, Any]:
    """The `pii` block from settings.yaml — the spaCy model name and score floor
    `adapters/pii/presidio_detector.py` runs with."""
    return dict(app_config().get("pii", {}))


def model_for(provider: str, purpose: str) -> str:
    """The ONLY way to obtain a model name. Never hardcode one in source.

    Raises ConfigError — not KeyError — when a provider declares no model for a
    purpose, so the router can treat it as a configuration problem and fail over
    rather than aborting the whole chain.
    """
    try:
        return str(providers_config()["providers"][provider]["models"][purpose])
    except KeyError as err:
        raise ConfigError(
            f"config/providers.yaml declares no '{purpose}' model for provider '{provider}'"
        ) from err


def provider_priority() -> list[str]:
    cfg = providers_config()
    mode = settings().taskflow_llm_mode
    try:
        return list(cfg["priority"][mode])
    except KeyError as err:
        raise ConfigError(
            f"config/providers.yaml declares no provider priority for mode '{mode}'"
        ) from err


def threshold_for(intent: str) -> float:
    """Auto-send threshold for an intent, falling back to `general` (BRD FR-8.3)."""
    thresholds = thresholds_config()["thresholds"]
    return float(thresholds.get(intent, thresholds["general"]))


def sla_minutes_for(intent: str) -> int:
    """Human-review SLA in minutes for an intent, falling back to `default`."""
    sla = thresholds_config()["sla_minutes"]
    return int(sla.get(intent, sla["default"]))


def budgets() -> dict[str, Any]:
    """The `budgets` block from providers.yaml."""
    return dict(providers_config().get("budgets", {}))
