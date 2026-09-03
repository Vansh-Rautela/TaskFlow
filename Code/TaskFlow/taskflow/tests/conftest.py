"""Shared fixtures.

The `hermetic_environment` fixture is autouse and session-wide, and it is the most
important thing in this file. Before it existed, running the suite against a
developer's populated `.env` dispatched **real** alert emails and Telegram messages
to production credentials, because `services.alert` reads `settings()` directly.
A test run must never touch a live account.
"""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskflow.adapters.db.engine import build_engine, build_session_factory
from taskflow.adapters.db.orm import Base

# Every credential the application can read. Blanking all of them makes each
# alert channel take its "not configured" branch and return False without a socket.
_CREDENTIAL_ENV = (
    "GMAIL_USER",
    "GMAIL_APP_PASSWORD",
    "OPS_EMAIL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ALERT_WEBHOOK_URL",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(scope="session", autouse=True)
def hermetic_environment() -> Iterator[None]:
    """Blank every credential for the whole session so no test can reach the network."""
    from taskflow.config import settings as settings_module

    saved = {key: os.environ.get(key) for key in _CREDENTIAL_ENV}
    for key in _CREDENTIAL_ENV:
        os.environ[key] = ""
    os.environ["TASKFLOW_API_TOKEN"] = "test-token"
    # Settings is lru_cached and .env is read at construction; drop the cached instance.
    settings_module.settings.cache_clear()

    yield

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    settings_module.settings.cache_clear()


@pytest.fixture
def api_token() -> str:
    return "test-token"


@pytest.fixture
def auth_headers(api_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_token}"}


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """An isolated in-memory database.

    `create_all` is acceptable here and only here: there is no migration history for
    a throwaway database to diverge from, and `tests/integration/test_migrations.py`
    asserts that this definition and Alembic's stay identical.
    """
    engine = build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield build_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def weights() -> dict[str, float]:
    return {
        "citation_coverage": 0.35,
        "grounding_entailment": 0.25,
        "retrieval_relevance": 0.20,
        "intent_confidence": 0.10,
        "tone_alignment": 0.10,
    }
