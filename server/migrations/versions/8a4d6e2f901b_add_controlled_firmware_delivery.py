"""add controlled firmware build and delivery

Revision ID: 8a4d6e2f901b
Revises: f38d9a7c2e10
Create Date: 2026-09-02 14:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8a4d6e2f901b"
down_revision: Union[str, Sequence[str], None] = "f38d9a7c2e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("repositories") as batch_op:
        batch_op.add_column(sa.Column("build_root", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("firmware_image_root", sa.Text(), nullable=True))

    with op.batch_alter_table("coding_runs") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum(
                "CREATED", "PREPARING", "PLANNING", "AWAITING_PLAN_APPROVAL", "IMPLEMENTING",
                "VALIDATING", "REPAIRING", "REVIEWING", "AWAITING_COMPLETION_APPROVAL",
                "COMPLETED", "FAILED", "BLOCKED", "CANCELLING", "CANCELLED",
                name="runstatus", native_enum=False,
            ),
            type_=sa.Enum(
                "CREATED", "PREPARING", "PLANNING", "AWAITING_PLAN_APPROVAL", "IMPLEMENTING",
                "BUILDING", "VALIDATING", "REPAIRING", "REVIEWING", "AWAITING_COMPLETION_APPROVAL",
                "COMPLETED", "FAILED", "BLOCKED", "CANCELLING", "CANCELLED",
                name="runstatus", native_enum=False,
            ),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "current_stage",
            existing_type=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            type_=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            existing_nullable=False,
        )

    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            type_=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            existing_nullable=False,
        )

    op.create_table(
        "firmware_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("coding_run_id", sa.String(length=36), nullable=False),
        sa.Column("partition", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["coding_run_id"], ["coding_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("coding_run_id", "partition", name="uq_firmware_artifact_run_partition"),
    )
    op.create_index(op.f("ix_firmware_artifacts_coding_run_id"), "firmware_artifacts", ["coding_run_id"])
    op.create_index(op.f("ix_firmware_artifacts_partition"), "firmware_artifacts", ["partition"])
    op.create_index(op.f("ix_firmware_artifacts_sha256"), "firmware_artifacts", ["sha256"])
    op.create_index(op.f("ix_firmware_artifacts_created_at"), "firmware_artifacts", ["created_at"])

    with op.batch_alter_table("qa_runs") as batch_op:
        batch_op.add_column(sa.Column("firmware_artifact_ids", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("qa_runs") as batch_op:
        batch_op.drop_column("firmware_artifact_ids")
    op.drop_index(op.f("ix_firmware_artifacts_created_at"), table_name="firmware_artifacts")
    op.drop_index(op.f("ix_firmware_artifacts_sha256"), table_name="firmware_artifacts")
    op.drop_index(op.f("ix_firmware_artifacts_partition"), table_name="firmware_artifacts")
    op.drop_index(op.f("ix_firmware_artifacts_coding_run_id"), table_name="firmware_artifacts")
    op.drop_table("firmware_artifacts")

    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            type_=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            existing_nullable=False,
        )

    with op.batch_alter_table("coding_runs") as batch_op:
        batch_op.alter_column(
            "current_stage",
            existing_type=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            type_=sa.Enum(
                "PREPARE", "PLAN", "IMPLEMENT", "VALIDATE", "REPAIR", "REVIEW", "COMPLETE",
                name="stagekind", native_enum=False,
            ),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum(
                "CREATED", "PREPARING", "PLANNING", "AWAITING_PLAN_APPROVAL", "IMPLEMENTING",
                "BUILDING", "VALIDATING", "REPAIRING", "REVIEWING", "AWAITING_COMPLETION_APPROVAL",
                "COMPLETED", "FAILED", "BLOCKED", "CANCELLING", "CANCELLED",
                name="runstatus", native_enum=False,
            ),
            type_=sa.Enum(
                "CREATED", "PREPARING", "PLANNING", "AWAITING_PLAN_APPROVAL", "IMPLEMENTING",
                "VALIDATING", "REPAIRING", "REVIEWING", "AWAITING_COMPLETION_APPROVAL",
                "COMPLETED", "FAILED", "BLOCKED", "CANCELLING", "CANCELLED",
                name="runstatus", native_enum=False,
            ),
            existing_nullable=False,
        )

    with op.batch_alter_table("repositories") as batch_op:
        batch_op.drop_column("firmware_image_root")
        batch_op.drop_column("build_root")
