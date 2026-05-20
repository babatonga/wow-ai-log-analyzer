"""add talent_ranking_snapshots cache table

The Talent-Finder fetches WCL ``characterRankings`` (100 players +
inline talents) to build its variant search. To avoid hitting the WCL
API on every run, the result is snapshotted in this table and refetched
only past a TTL.

Revision ID: 0020_talent_ranking_snapshots
Revises: 0019_simulation_mode
Create Date: 2026-05-20 16:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_talent_ranking_snapshots"
down_revision: Union[str, None] = "0019_simulation_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "talent_ranking_snapshots",
        sa.Column("spec_slug", sa.String(48), nullable=False),
        sa.Column("encounter_id", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "rankings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("spec_slug", "encounter_id"),
    )


def downgrade() -> None:
    op.drop_table("talent_ranking_snapshots")
