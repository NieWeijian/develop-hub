"""add test case risk levels

Revision ID: c94518f2b7a4
Revises: a63d55c910b2
Create Date: 2026-09-02 10:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c94518f2b7a4"
down_revision: Union[str, Sequence[str], None] = "a63d55c910b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    risk_enum = sa.Enum("READ_ONLY", "STATE_CHANGE", "FIRMWARE_WRITE", name="testcaserisk", native_enum=False)
    with op.batch_alter_table("test_cases") as batch_op:
        batch_op.add_column(
            sa.Column("risk_level", risk_enum, nullable=False, server_default="READ_ONLY")
        )
    with op.batch_alter_table("qa_case_executions") as batch_op:
        batch_op.add_column(
            sa.Column("risk_level", risk_enum, nullable=False, server_default="READ_ONLY")
        )


def downgrade() -> None:
    with op.batch_alter_table("qa_case_executions") as batch_op:
        batch_op.drop_column("risk_level")
    with op.batch_alter_table("test_cases") as batch_op:
        batch_op.drop_column("risk_level")
