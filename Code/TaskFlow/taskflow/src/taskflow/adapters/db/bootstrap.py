"""Database bootstrap.

Alembic is the single source of truth for the schema. Nothing in this application
calls `Base.metadata.create_all` against a real database: two mechanisms that can
both create tables is two schemas that can disagree, and the one that wins is
whichever ran first.

Tests may still use `create_all` against a throwaway in-memory database — there is
no migration history to diverge from there — and `tests/integration/test_migrations.py`
asserts the two definitions stay identical.
"""

import asyncio
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config

logger = structlog.get_logger()

PROJECT_ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def run_migrations(database_url: str) -> None:
    """Upgrade the database at `database_url` to the current Alembic head."""
    if not ALEMBIC_INI.exists():
        raise FileNotFoundError(f"alembic.ini not found at {ALEMBIC_INI}")

    # Ensure a file-backed SQLite database has a directory to live in.
    if database_url.startswith("sqlite") and ":memory:" not in database_url:
        db_path = Path(database_url.split("///")[-1])
        db_path.parent.mkdir(parents=True, exist_ok=True)

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option(
        "script_location", str(PROJECT_ROOT / "src/taskflow/adapters/db/migrations")
    )
    cfg.set_main_option("sqlalchemy.url", database_url.replace("sqlite+aiosqlite", "sqlite"))

    command.upgrade(cfg, "head")
    logger.info("database_migrated_to_head", url=_redact(database_url))


async def run_migrations_async(database_url: str) -> None:
    """`run_migrations` for callers that are already inside an event loop.

    Alembic's env.py drives the async engine with `asyncio.run()`, which raises if a
    loop is already running — as it is inside FastAPI's lifespan. Running it on a
    worker thread gives it the fresh loop it expects.
    """
    await asyncio.to_thread(run_migrations, database_url)


def _redact(url: str) -> str:
    """Strip any credential from a database URL before it reaches a log."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url
