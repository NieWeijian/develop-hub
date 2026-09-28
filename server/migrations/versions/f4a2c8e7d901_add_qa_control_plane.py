"""add 3588 QA control plane

Revision ID: f4a2c8e7d901
Revises: e2bd02b5337d
Create Date: 2026-09-01 17:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4a2c8e7d901"
down_revision: Union[str, Sequence[str], None] = "e2bd02b5337d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "test_cases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("expected_exit_code", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_test_cases_code"), "test_cases", ["code"], unique=True)
    op.create_table(
        "qa_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("display_number", sa.Integer(), nullable=False),
        sa.Column("coding_run_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "CREATED", "PREPARING", "TESTING", "REPORTING", "AWAITING_APPROVAL",
                "PASSED", "FAILED", "BLOCKED", name="qarunstatus", native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("handoff", sa.JSON(), nullable=False),
        sa.Column("firmware_path", sa.Text(), nullable=True),
        sa.Column("firmware_sha256", sa.String(length=64), nullable=True),
        sa.Column("ota_partitions", sa.JSON(), nullable=False),
        sa.Column("perform_ota", sa.Boolean(), nullable=False),
        sa.Column("remote_workspace", sa.Text(), nullable=True),
        sa.Column("codex_thread_id", sa.String(length=120), nullable=True),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["coding_run_id"], ["coding_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("display_number"),
    )
    op.create_index(op.f("ix_qa_runs_coding_run_id"), "qa_runs", ["coding_run_id"], unique=False)
    op.create_index(op.f("ix_qa_runs_codex_thread_id"), "qa_runs", ["codex_thread_id"], unique=False)
    op.create_index(op.f("ix_qa_runs_created_at"), "qa_runs", ["created_at"], unique=False)
    op.create_index(op.f("ix_qa_runs_status"), "qa_runs", ["status"], unique=False)
    op.create_table(
        "qa_case_executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("qa_run_id", sa.String(length=36), nullable=False),
        sa.Column("test_case_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "RUNNING", "PASSED", "FAILED", "SKIPPED", name="qacasestatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout_tail", sa.Text(), nullable=True),
        sa.Column("stderr_tail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["qa_run_id"], ["qa_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["test_case_id"], ["test_cases.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("qa_run_id", "test_case_id", name="uq_qa_run_test_case"),
    )
    op.create_index(op.f("ix_qa_case_executions_qa_run_id"), "qa_case_executions", ["qa_run_id"], unique=False)
    op.create_index(op.f("ix_qa_case_executions_test_case_id"), "qa_case_executions", ["test_case_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_qa_case_executions_test_case_id"), table_name="qa_case_executions")
    op.drop_index(op.f("ix_qa_case_executions_qa_run_id"), table_name="qa_case_executions")
    op.drop_table("qa_case_executions")
    op.drop_index(op.f("ix_qa_runs_status"), table_name="qa_runs")
    op.drop_index(op.f("ix_qa_runs_created_at"), table_name="qa_runs")
    op.drop_index(op.f("ix_qa_runs_codex_thread_id"), table_name="qa_runs")
    op.drop_index(op.f("ix_qa_runs_coding_run_id"), table_name="qa_runs")
    op.drop_table("qa_runs")
    op.drop_index(op.f("ix_test_cases_code"), table_name="test_cases")
    op.drop_table("test_cases")
