"""add reusable worktree branch mode

Revision ID: f38d9a7c2e10
Revises: d12b77e48f91
Create Date: 2026-09-02 11:15:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f38d9a7c2e10"
down_revision: Union[str, Sequence[str], None] = "d12b77e48f91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("coding_runs") as batch_op:
        batch_op.alter_column(
            "worktree_mode",
            existing_type=sa.String(length=8),
            type_=sa.Enum(
                "NEW",
                "EXISTING",
                "REUSE_NEW_BRANCH",
                name="worktreemode",
                native_enum=False,
                length=32,
            ),
            existing_nullable=False,
            existing_server_default="NEW",
        )
        batch_op.add_column(sa.Column("worktree_initial_branch", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("coding_runs") as batch_op:
        batch_op.drop_column("worktree_initial_branch")
        batch_op.alter_column(
            "worktree_mode",
            existing_type=sa.String(length=32),
            type_=sa.Enum("NEW", "EXISTING", name="worktreemode", native_enum=False),
            existing_nullable=False,
            existing_server_default="NEW",
        )
