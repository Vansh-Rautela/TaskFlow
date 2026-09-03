"""Alembic env.py — wired to taskflow ORM metadata and settings.

The database URL comes from the TASKFLOW_DATABASE_URL environment variable
(or .env file) via taskflow.config.settings, not from alembic.ini.
This keeps secrets out of version control.
"""

import asyncio
import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool

# Wire up our ORM Base so autogenerate can detect schema changes.
from taskflow.adapters.db.orm import Base

target_metadata = Base.metadata

# Alembic Config object
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the database URL, in precedence order:
#   1. whatever the caller already set on the Config (adapters/db/bootstrap.py does
#      this, and it must win — otherwise programmatic callers silently migrate the
#      database named in the environment instead of the one they asked for);
#   2. DATABASE_URL from the environment, for the `alembic` CLI;
#   3. the project default.
_db_url = (
    config.get_main_option("sqlalchemy.url")
    or os.environ.get("DATABASE_URL")
    or "sqlite+aiosqlite:///./data/taskflow.db"
)

# Alembic's sync runner needs a sync driver; swap aiosqlite -> pysqlite.
_sync_url = _db_url.replace("sqlite+aiosqlite", "sqlite")
config.set_main_option("sqlalchemy.url", _sync_url)

# The async runner needs the async driver back.
_async_url = _db_url if "+aiosqlite" in _db_url else _db_url.replace("sqlite:", "sqlite+aiosqlite:")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generates SQL, no DB connection needed)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Any) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against the live async engine."""
    from sqlalchemy.ext.asyncio import create_async_engine

    connectable = create_async_engine(
        _async_url,
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
