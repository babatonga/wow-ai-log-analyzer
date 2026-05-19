"""add ``mode`` to simulations

A new sim mode lets us distinguish the original standard simulation
flow from the Talent-Finder flow (which is one POST that fans out into
dozens of variant runs). The column is a free-form string so future
modes can be added without another migration.

Revision ID: 0019_simulation_mode
Revises: 0018_simulation_custom_overrides
Create Date: 2026-05-20 12:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_simulation_mode"
down_revision: Union[str, None] = "0018_simulation_custom_overrides"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "simulations",
        sa.Column(
            "mode",
            sa.String(32),
            nullable=False,
            server_default="standard",
        ),
    )
    op.create_index(
        "ix_simulations_mode",
        "simulations",
        ["mode"],
    )


def downgrade() -> None:
    op.drop_index("ix_simulations_mode", table_name="simulations")
    op.drop_column("simulations", "mode")
