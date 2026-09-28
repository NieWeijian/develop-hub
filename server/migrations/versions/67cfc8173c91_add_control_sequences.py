"""add persistent control-plane sequences

Revision ID: 67cfc8173c91
Revises: b8f1d66107d2
Create Date: 2026-08-28 15:38:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "67cfc8173c91"
down_revision: Union[str, Sequence[str], None] = "b8f1d66107d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "control_sequences",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("next_value", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )
    op.execute(
        """
        INSERT INTO control_sequences (name, next_value)
        SELECT 'coding_run_display', COALESCE(MAX(display_number), 0) + 1
        FROM coding_runs
        """
    )


def downgrade() -> None:
    op.drop_table("control_sequences")
