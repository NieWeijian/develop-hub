"""Snapshot bench identity and delivery evidence without rewriting historical rounds."""
from alembic import op
import sqlalchemy as sa
revision = '71ad9b20e451'
down_revision = '4c6f2a9d8e10'
branch_labels = None
depends_on = None

INDEXES = [('gerrit_repositories', 'project_path'), ('sdk_repositories', 'name'), ('test_benches', 'name'), ('test_rounds', 'display_number')]

def upgrade():
    with op.batch_alter_table("test_products") as batch:
        batch.drop_constraint("uq_test_product_sdk_repo", type_="unique")
    # Align the existing unique model indexes with their historical migrations.
    for table, column in INDEXES:
        op.drop_index(f'ix_{table}_{column}', table_name=table)
        op.create_index(f'ix_{table}_{column}', table, [column], unique=True)
    op.add_column('test_rounds', sa.Column('bench_snapshot', sa.JSON(), nullable=False, server_default='{}'))
    op.add_column('test_rounds', sa.Column('delivery_result', sa.JSON(), nullable=True))
    # Empty legacy snapshots are deliberately not reconstructed from mutable current nodes.

def downgrade():
    with op.batch_alter_table("test_products") as batch:
        batch.create_unique_constraint("uq_test_product_sdk_repo", ["sdk_repository_id", "tested_repository_id"])
    for table, column in INDEXES:
        op.drop_index(f'ix_{table}_{column}', table_name=table)
        op.create_index(f'ix_{table}_{column}', table, [column], unique=False)
    op.drop_column('test_rounds', 'delivery_result')
    op.drop_column('test_rounds', 'bench_snapshot')
