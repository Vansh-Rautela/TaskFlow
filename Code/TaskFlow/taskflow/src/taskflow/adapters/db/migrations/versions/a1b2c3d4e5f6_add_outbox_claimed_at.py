"""Add outbox.claimed_at for the delivery worker lease

Revision ID: a1b2c3d4e5f6
Revises: d300852af25a
Create Date: 2026-08-25

A worker stamps `claimed_at` when it moves a row QUEUED -> SENDING. A row still in
SENDING after `worker.claim_lease_minutes` is assumed orphaned by a crashed worker
and returned to QUEUED, so a delivery is never silently stranded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "d300852af25a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("claimed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox", "claimed_at")
