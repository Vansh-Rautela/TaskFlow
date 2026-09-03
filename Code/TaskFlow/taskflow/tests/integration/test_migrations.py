"""Migrations are the single source of truth for the schema.

The API and dashboard used to call `Base.metadata.create_all` at startup, so a
database could end up with whatever the ORM happened to declare rather than what the
migration history describes. `create_all` now appears only in tests, and this module
is what keeps the two definitions honest.
"""

import pytest
from sqlalchemy import create_engine, inspect

from taskflow.adapters.db.bootstrap import run_migrations
from taskflow.adapters.db.orm import Base

pytestmark = pytest.mark.integration


@pytest.fixture
def migrated_inspector(tmp_path):
    db = tmp_path / "migrated.db"
    run_migrations(f"sqlite+aiosqlite:///{db}")
    return inspect(create_engine(f"sqlite:///{db}"))


@pytest.fixture
def orm_inspector():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return inspect(engine)


def test_migrations_and_orm_declare_the_same_tables(migrated_inspector, orm_inspector):
    migrated = set(migrated_inspector.get_table_names()) - {"alembic_version"}
    declared = set(orm_inspector.get_table_names())
    assert migrated == declared, (
        f"only in migrations: {migrated - declared}; only in the ORM: {declared - migrated}"
    )


def test_migrations_and_orm_declare_the_same_columns(migrated_inspector, orm_inspector):
    for table in sorted(set(orm_inspector.get_table_names())):
        migrated = {c["name"] for c in migrated_inspector.get_columns(table)}
        declared = {c["name"] for c in orm_inspector.get_columns(table)}
        assert migrated == declared, (
            f"{table}: only in migrations {migrated - declared}, only in ORM {declared - migrated}"
        )


def test_safety_critical_unique_constraints_survive_migration(migrated_inspector):
    """`inbox.dedupe_key` is the deduplication guarantee and `outbox.idempotency_key`
    is the send-once guarantee. Both must be enforced by the database, not by code."""
    for table, column in (("inbox", "dedupe_key"), ("outbox", "idempotency_key")):
        uniques = {
            tuple(u["column_names"]) for u in migrated_inspector.get_unique_constraints(table)
        }
        indexed = {
            tuple(i["column_names"]) for i in migrated_inspector.get_indexes(table) if i["unique"]
        }
        assert (column,) in uniques | indexed, f"{table}.{column} is not UNIQUE after migration"


def test_every_table_carries_a_tenant_id(migrated_inspector):
    """Every persisted row carries a tenant_id (see ADR-004 and the migration rules)."""
    for table in sorted(set(migrated_inspector.get_table_names()) - {"alembic_version"}):
        columns = {c["name"] for c in migrated_inspector.get_columns(table)}
        assert "tenant_id" in columns, f"{table} has no tenant_id column"


def test_migrations_round_trip(tmp_path):
    """Every revision has a working downgrade; two of them used to drop and recreate
    indexes on tables they never touched."""
    from alembic import command
    from alembic.config import Config

    from taskflow.adapters.db.bootstrap import ALEMBIC_INI, PROJECT_ROOT

    db = tmp_path / "roundtrip.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option(
        "script_location", str(PROJECT_ROOT / "src/taskflow/adapters/db/migrations")
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    remaining = set(inspect(create_engine(f"sqlite:///{db}")).get_table_names())
    assert "outbox" in remaining and "reviews" in remaining
