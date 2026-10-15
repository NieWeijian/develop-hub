"""Initialize the independent RK testing schema.

Use ``alembic upgrade testing@head`` for a fresh testing installation. This
branch does not read, transform or remove any legacy control-plane records.
"""

from alembic import op
import sqlalchemy as sa

revision = "rk_testing_20261013"
down_revision = None
branch_labels = ("testing",)
depends_on = None


def identity():
    return sa.Column("id", sa.String(36), primary_key=True)


def created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def data(name):
    return sa.Column(name, sa.JSON(), nullable=False)


def upgrade():
    op.create_table("testing_products", identity(), sa.Column("name", sa.String(160), nullable=False),
        sa.Column("chip", sa.String(80), nullable=False), sa.Column("board", sa.String(160), nullable=False),
        data("capabilities"), data("config"), sa.Column("enabled", sa.Boolean(), nullable=False), created())
    op.create_table("testing_build_environments", identity(), sa.Column("name", sa.String(160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), data("node"), data("config"),
        sa.Column("enabled", sa.Boolean(), nullable=False), created())
    op.create_table("testing_devices", identity(), sa.Column("name", sa.String(160), nullable=False),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("testing_products.id", ondelete="RESTRICT"), nullable=False),
        data("node"), data("capabilities"), data("config"), sa.Column("health", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False), created())
    op.create_index("ix_testing_devices_product_id", "testing_devices", ["product_id"])
    op.create_table("testing_input_assets", identity(), sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("name", sa.String(160), nullable=False), sa.Column("digest", sa.String(128), nullable=False),
        data("manifest"), created())
    op.create_index("ix_testing_input_assets_kind", "testing_input_assets", ["kind"])
    op.create_table("testing_case_revisions", identity(), sa.Column("case_id", sa.String(160), nullable=False),
        sa.Column("version", sa.String(80), nullable=False), sa.Column("name", sa.String(160), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False), data("definition"), sa.Column("enabled", sa.Boolean(), nullable=False),
        created(), sa.UniqueConstraint("case_id", "version", name="uq_testing_case_version"))
    op.create_index("ix_testing_case_revisions_case_id", "testing_case_revisions", ["case_id"])
    op.create_table("testing_plans", identity(), sa.Column("name", sa.String(160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False), data("draft"), sa.Column("archived", sa.Boolean(), nullable=False),
        created(), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("testing_plan_revisions", identity(),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("testing_plans.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), data("config"), data("resolved"), created(),
        sa.UniqueConstraint("plan_id", "version", name="uq_testing_plan_version"))
    op.create_index("ix_testing_plan_revisions_plan_id", "testing_plan_revisions", ["plan_id"])
    op.create_table("testing_runs", identity(),
        sa.Column("plan_revision_id", sa.String(36), sa.ForeignKey("testing_plan_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("request_digest", sa.String(64), nullable=False), sa.Column("state", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(40), nullable=False), sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("recovery", sa.String(32), nullable=False), data("request_snapshot"),
        sa.Column("resolved_input", sa.JSON(), nullable=True), data("result"), data("events"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False), sa.Column("operator_revision", sa.Integer(), nullable=False),
        created(), sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_testing_runs_plan_revision_id", "testing_runs", ["plan_revision_id"])
    op.create_index("ix_testing_runs_state", "testing_runs", ["state"])
    op.create_table("testing_operator_records", identity(),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("testing_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("step_id", sa.String(320), nullable=False), sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False), sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("operator", sa.String(160), nullable=False), sa.Column("decision", sa.String(24), nullable=False),
        data("values"), data("attachments"), created(),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_testing_operator_request"),
        sa.UniqueConstraint("run_id", "revision", name="uq_testing_operator_revision"),
        sa.UniqueConstraint("run_id", "step_id", name="uq_testing_operator_step"))
    op.create_index("ix_testing_operator_records_run_id", "testing_operator_records", ["run_id"])


def downgrade():
    for name in ("testing_operator_records", "testing_runs", "testing_plan_revisions", "testing_plans",
                 "testing_case_revisions", "testing_input_assets", "testing_devices", "testing_build_environments", "testing_products"):
        op.drop_table(name)
