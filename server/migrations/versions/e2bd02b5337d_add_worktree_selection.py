"""add worktree selection metadata

Revision ID: e2bd02b5337d
Revises: 67cfc8173c91
Create Date: 2026-09-01 10:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2bd02b5337d"
down_revision: Union[str, Sequence[str], None] = "67cfc8173c91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "coding_runs",
        sa.Column(
            "worktree_mode",
            sa.Enum("NEW", "EXISTING", name="worktreemode", native_enum=False),
            nullable=False,
            server_default="NEW",
        ),
    )
    op.add_column(
        "coding_runs",
        sa.Column("worktree_branch", sa.String(length=255), nullable=False, server_default=""),
    )
    op.execute("UPDATE coding_runs SET worktree_branch = work_branch WHERE worktree_branch = ''")


def downgrade() -> None:
    op.drop_column("coding_runs", "worktree_branch")
    op.drop_column("coding_runs", "worktree_mode")
