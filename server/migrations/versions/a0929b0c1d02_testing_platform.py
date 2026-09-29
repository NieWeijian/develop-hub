"""Versioned release validation and persistent scheduled tests."""
from alembic import op
import sqlalchemy as sa

revision = 'a0929b0c1d02'
down_revision = '31ed7094a820'
branch_labels = None
depends_on = None

def upgrade():
    def ident(): return sa.Column('id', sa.String(36), primary_key=True)
    def created(): return sa.Column('created_at', sa.DateTime(timezone=True), nullable=False)
    op.create_table('release_candidates', ident(),
        sa.Column('product_id', sa.String(36), sa.ForeignKey('test_products.id'), nullable=False, index=True),
        sa.Column('source_round_id', sa.String(36), sa.ForeignKey('test_rounds.id'), nullable=False),
        sa.Column('name', sa.String(160), nullable=False), sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False), created(),
        sa.UniqueConstraint('product_id','name', name='uq_release_candidate_name'))
    op.create_table('release_plans', ident(),
        sa.Column('candidate_id', sa.String(36), sa.ForeignKey('release_candidates.id'), nullable=False, index=True),
        sa.Column('name', sa.String(160), nullable=False), sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('requirements', sa.JSON(), nullable=False), sa.Column('final_report', sa.JSON(), nullable=True), created())
    op.create_table('validation_evidence', ident(),
        sa.Column('plan_id', sa.String(36), sa.ForeignKey('release_plans.id'), nullable=False, index=True),
        sa.Column('requirement_id', sa.String(36), nullable=False), sa.Column('requirement_hash', sa.String(64), nullable=False),
        sa.Column('round_id', sa.String(36), sa.ForeignKey('test_rounds.id'), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=False), created())
    op.create_table('test_schedules', ident(), sa.Column('name', sa.String(160), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False), sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('next_fire_at', sa.DateTime(timezone=True), nullable=False, index=True), created())
    op.create_table('schedule_runs', ident(),
        sa.Column('schedule_id', sa.String(36), sa.ForeignKey('test_schedules.id'), nullable=False, index=True),
        sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=False), sa.Column('state', sa.String(32), nullable=False),
        sa.Column('message', sa.Text(), nullable=False), sa.Column('source_commit', sa.String(64), nullable=False),
        sa.Column('round_id', sa.String(36), sa.ForeignKey('test_rounds.id'), nullable=True), created(),
        sa.UniqueConstraint('schedule_id','scheduled_at', name='uq_schedule_occurrence'))
    with op.batch_alter_table('test_rounds') as batch:
        batch.add_column(sa.Column('origin', sa.String(20), nullable=False, server_default='manual'))
        batch.add_column(sa.Column('candidate_id', sa.String(36), nullable=True))
        batch.add_column(sa.Column('validation_plan_id', sa.String(36), nullable=True))
        batch.add_column(sa.Column('schedule_id', sa.String(36), nullable=True))
    import json
    bind=op.get_bind()
    for rid,snapshot in bind.execute(sa.text('SELECT id, product_snapshot FROM test_rounds')):
        snapshot=json.loads(snapshot) if isinstance(snapshot,str) else snapshot
        if (snapshot or {}).get('developer_mode'):
            bind.execute(sa.text("UPDATE test_rounds SET origin='quick' WHERE id=:id"), {'id':rid})

def downgrade():
    raise RuntimeError('Release evidence and scheduling history must be retained')
