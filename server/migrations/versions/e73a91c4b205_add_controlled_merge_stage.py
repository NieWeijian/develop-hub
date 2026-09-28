"""add controlled merge stage

Revision ID: e73a91c4b205
Revises: c4b80f1a72de
Create Date: 2026-09-02 18:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e73a91c4b205"
down_revision: Union[str, Sequence[str], None] = "c4b80f1a72de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_VALUES = (
    "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "DISCUSS", "COMPLETE",
)
NEW_VALUES = (
    "PREPARE", "PLAN", "IMPLEMENT", "BUILD", "VALIDATE", "REPAIR", "REVIEW", "DISCUSS", "MERGE", "COMPLETE",
)


def _enum(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name="stagekind", native_enum=False)


def upgrade() -> None:
    with op.batch_alter_table("coding_runs") as batch_op:
        batch_op.alter_column(
            "current_stage", existing_type=_enum(OLD_VALUES), type_=_enum(NEW_VALUES), existing_nullable=False
        )
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.alter_column(
            "kind", existing_type=_enum(OLD_VALUES), type_=_enum(NEW_VALUES), existing_nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("stage_runs") as batch_op:
        batch_op.alter_column(
            "kind", existing_type=_enum(NEW_VALUES), type_=_enum(OLD_VALUES), existing_nullable=False
        )
    with op.batch_alter_table("coding_runs") as batch_op:
        batch_op.alter_column(
            "current_stage", existing_type=_enum(NEW_VALUES), type_=_enum(OLD_VALUES), existing_nullable=False
        )
