"""Store QA demo execution snapshots.

Revision ID: 2ae9c14d507b
Revises: 93ad11b5e832
"""
from alembic import op
import sqlalchemy as sa

revision = "2ae9c14d507b"
down_revision = "93ad11b5e832"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "qa_demo_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("bench_id", sa.String(36), sa.ForeignKey("test_benches.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("remote_job_id", sa.String(80), nullable=True, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("request_snapshot", sa.JSON(), nullable=False),
        sa.Column("dispatch_result", sa.JSON(), nullable=True),
        sa.Column("controller_result", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_qa_demo_runs_bench_id", "qa_demo_runs", ["bench_id"])
    op.create_index("ix_qa_demo_runs_status", "qa_demo_runs", ["status"])
    op.create_index("ix_qa_demo_runs_created_at", "qa_demo_runs", ["created_at"])

def downgrade():
    op.drop_index("ix_qa_demo_runs_created_at", table_name="qa_demo_runs")
    op.drop_index("ix_qa_demo_runs_status", table_name="qa_demo_runs")
    op.drop_index("ix_qa_demo_runs_bench_id", table_name="qa_demo_runs")
    op.drop_table("qa_demo_runs")
