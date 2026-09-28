"""Allow current firmware rounds without a Gerrit point."""
from alembic import op
import sqlalchemy as sa
revision='31ed7094a820'
down_revision='2ae9c14d507b'
branch_labels=None
depends_on=None
def upgrade():
    with op.batch_alter_table('test_rounds') as batch:
        batch.alter_column('test_point_id',existing_type=sa.String(36),nullable=True)
def downgrade():
    raise RuntimeError('Preserve developer round history; nullable FK migration is not automatically reversible')
