"""clear legacy coding-run QA data

Revision ID: 2bd71a6e94c3
Revises: 19a7c4d8e2f0
Create Date: 2026-09-11 09:20:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2bd71a6e94c3"
down_revision: Union[str, Sequence[str], None] = "19a7c4d8e2f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Product-driven Test Rounds supersede legacy Coding Run handoffs. Keep the
    # reusable Test Case catalog, but discard execution/report and plugin data.
    op.execute(sa.text("UPDATE coding_runs SET source_qa_run_id = NULL WHERE source_qa_run_id IS NOT NULL"))
    op.execute(sa.text("DELETE FROM qa_case_executions"))
    op.execute(sa.text("DELETE FROM qa_runs"))
    op.execute(sa.text("DELETE FROM test_cases WHERE plugin_release_id IS NOT NULL"))
    op.execute(sa.text("DELETE FROM qa_plugin_releases"))
    op.execute(sa.text("DELETE FROM control_sequences WHERE name = 'qa_run_display'"))


def downgrade() -> None:
    # Removed execution history cannot be reconstructed safely.
    pass
