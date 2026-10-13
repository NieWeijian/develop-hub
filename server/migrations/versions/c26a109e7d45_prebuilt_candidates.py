"""Candidates may originate from existing SDK images without a fabricated build round."""
from alembic import op
import sqlalchemy as sa
revision = 'c26a109e7d45'
down_revision = 'b1048e2a9f31'
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table('release_candidates') as batch:
        batch.alter_column('source_round_id', existing_type=sa.String(36), nullable=True)

def downgrade():
    raise RuntimeError('Preserve imported candidate evidence; do not discard candidates without build rounds')
