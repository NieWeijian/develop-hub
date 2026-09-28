"""add requirement closed timestamp

Revision ID: f905c62d1a48
Revises: e73a91c4b205
Create Date: 2026-09-02 18:25:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f905c62d1a48"
down_revision: Union[str, Sequence[str], None] = "e73a91c4b205"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("coding_runs") as batch_op:
        batch_op.add_column(sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_coding_runs_closed_at", ["closed_at"])
    op.execute(
        sa.text(
            """
            UPDATE coding_runs
            SET closed_at = (
                SELECT MAX(stage_runs.finished_at)
                FROM stage_runs
                WHERE stage_runs.run_id = coding_runs.id
                  AND stage_runs.kind = 'MERGE'
                  AND stage_runs.status = 'SUCCEEDED'
            )
            WHERE EXISTS (
                SELECT 1 FROM stage_runs
                WHERE stage_runs.run_id = coding_runs.id
                  AND stage_runs.kind = 'MERGE'
                  AND stage_runs.status = 'SUCCEEDED'
            )
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("coding_runs") as batch_op:
        batch_op.drop_index("ix_coding_runs_closed_at")
        batch_op.drop_column("closed_at")
