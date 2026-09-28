"""add coding follow-up discussions

Revision ID: c4b80f1a72de
Revises: 8a4d6e2f901b
Create Date: 2026-09-02 16:20:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4b80f1a72de"
down_revision: Union[str, Sequence[str], None] = "8a4d6e2f901b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("coding_runs") as batch_op:
        batch_op.add_column(sa.Column("parent_run_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("source_qa_run_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_coding_runs_parent_run_id",
            "coding_runs",
            ["parent_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_coding_runs_source_qa_run_id",
            "qa_runs",
            ["source_qa_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.alter_column(
            "current_stage",
            existing_type=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            type_=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "DISCUSS", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            existing_nullable=False,
        )
        batch_op.create_index("ix_coding_runs_parent_run_id", ["parent_run_id"])
        batch_op.create_index("ix_coding_runs_source_qa_run_id", ["source_qa_run_id"])

    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            type_=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "DISCUSS", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "DISCUSS", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            type_=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            existing_nullable=False,
        )

    with op.batch_alter_table("coding_runs") as batch_op:
        batch_op.drop_index("ix_coding_runs_source_qa_run_id")
        batch_op.drop_index("ix_coding_runs_parent_run_id")
        batch_op.alter_column(
            "current_stage",
            existing_type=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "DISCUSS", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            type_=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            existing_nullable=False,
        )
        batch_op.drop_constraint("fk_coding_runs_source_qa_run_id", type_="foreignkey")
        batch_op.drop_constraint("fk_coding_runs_parent_run_id", type_="foreignkey")
        batch_op.drop_column("source_qa_run_id")
        batch_op.drop_column("parent_run_id")
