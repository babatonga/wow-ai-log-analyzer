"""add reasoning_effort column to user_ai_configs

Lets each BYOK user pick OpenAI's ``reasoning_effort`` setting
(``minimal | low | medium | high``) per their own API config. NULL means
"use OpenAI's default" — for Chat Completions that's effectively no
reasoning.

Revision ID: 0012_user_ai_reasoning_effort
Revises: 0011_analysis_uses_byok
Create Date: 2026-05-10 00:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_user_ai_reasoning_effort"
down_revision: Union[str, None] = "0011_analysis_uses_byok"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_ai_configs",
        sa.Column("reasoning_effort", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_ai_configs", "reasoning_effort")
