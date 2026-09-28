"""snapshot QA test cases for immutable audit

Revision ID: a63d55c910b2
Revises: f4a2c8e7d901
Create Date: 2026-09-01 17:43:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a63d55c910b2"
down_revision: Union[str, Sequence[str], None] = "f4a2c8e7d901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("qa_case_executions") as batch_op:
        batch_op.add_column(sa.Column("case_code", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("case_name", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("case_description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("command", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("expected_exit_code", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("timeout_seconds", sa.Integer(), nullable=True))

    op.execute(
        sa.text(
            """
            UPDATE qa_case_executions
            SET case_code = (SELECT code FROM test_cases WHERE test_cases.id = qa_case_executions.test_case_id),
                case_name = (SELECT name FROM test_cases WHERE test_cases.id = qa_case_executions.test_case_id),
                case_description = (SELECT description FROM test_cases WHERE test_cases.id = qa_case_executions.test_case_id),
                command = (SELECT command FROM test_cases WHERE test_cases.id = qa_case_executions.test_case_id),
                expected_exit_code = (SELECT expected_exit_code FROM test_cases WHERE test_cases.id = qa_case_executions.test_case_id),
                timeout_seconds = (SELECT timeout_seconds FROM test_cases WHERE test_cases.id = qa_case_executions.test_case_id)
            """
        )
    )

    with op.batch_alter_table("qa_case_executions") as batch_op:
        batch_op.alter_column("case_code", existing_type=sa.String(length=80), nullable=False)
        batch_op.alter_column("case_name", existing_type=sa.String(length=200), nullable=False)
        batch_op.alter_column("case_description", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("command", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("expected_exit_code", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("timeout_seconds", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("qa_case_executions") as batch_op:
        batch_op.drop_column("timeout_seconds")
        batch_op.drop_column("expected_exit_code")
        batch_op.drop_column("command")
        batch_op.drop_column("case_description")
        batch_op.drop_column("case_name")
        batch_op.drop_column("case_code")
