"""Persistent, manually gated hardware scenarios."""
from alembic import op
import sqlalchemy as sa
revision = 'b1048e2a9f31'
down_revision = 'a0929b0c1d02'
branch_labels = None
depends_on = None
def upgrade():
    op.create_table('special_runs',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('scenario_code',sa.String(80),nullable=False,index=True),
        sa.Column('product_id',sa.String(36),sa.ForeignKey('test_products.id',ondelete='RESTRICT'),nullable=False,index=True),
        sa.Column('bench_id',sa.String(36),sa.ForeignKey('test_benches.id',ondelete='RESTRICT'),nullable=False,index=True),
        sa.Column('status',sa.String(32),nullable=False,index=True),
        sa.Column('revision',sa.Integer(),nullable=False),
        sa.Column('current_step_index',sa.Integer(),nullable=False),
        sa.Column('bench_reserved',sa.Boolean(),nullable=False,index=True),
        *[sa.Column(name,sa.JSON(),nullable=False) for name in ('definition_snapshot','product_snapshot','bench_snapshot','device_snapshot','tool_snapshot','steps','events','context')],
        sa.Column('remote_path',sa.Text(),nullable=False),
        sa.Column('last_error',sa.Text(),nullable=True),
        sa.Column('created_by',sa.String(120),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,index=True),
        sa.Column('completed_at',sa.DateTime(timezone=True),nullable=True))
def downgrade():
    raise RuntimeError('Operator and hardware evidence must be retained')
