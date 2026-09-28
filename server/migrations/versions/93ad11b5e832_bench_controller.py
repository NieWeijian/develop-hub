"""Persist remote Controller state."""
from alembic import op
import sqlalchemy as sa
revision="93ad11b5e832"
down_revision="82bc10a4d721"
branch_labels=None
depends_on=None

def upgrade():
    op.add_column("test_rounds",sa.Column("controller_result",sa.JSON(),nullable=True))

def downgrade():
    op.execute("UPDATE test_rounds SET status='BLOCKED' WHERE status IN ('BENCH_RUNNING','FLASH_COMPLETE','BENCH_FAILED')")
    op.drop_column("test_rounds","controller_result")
