"""user_ai_configs: enable_thinking + max_output_tokens

BYOK users get two new optional knobs:

- ``enable_thinking`` (openai_compatible only): per-user override of the
  app-wide LOCAL_AI_ENABLE_THINKING for their own self-hosted server.
- ``max_output_tokens``: per-user output budget / cost cap, overriding
  the app-wide AI_MAX_TOKENS default.

Both nullable — NULL means "inherit the app-wide setting", so existing
rows keep their behaviour unchanged.

Revision ID: 0019_user_ai_extra_params
Revises: 0018_simulation_custom_overrides
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_user_ai_extra_params"
down_revision: Union[str, None] = "0018_simulation_custom_overrides"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_ai_configs",
        sa.Column("enable_thinking", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "user_ai_configs",
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_ai_configs", "max_output_tokens")
    op.drop_column("user_ai_configs", "enable_thinking")
