"""Persist test case sets, Patchset upload time, and close historical stage events."""
from alembic import op
import sqlalchemy as sa
import json

revision = "82bc10a4d721"
down_revision = "71ad9b20e451"
branch_labels = None
depends_on = None


def upgrade():
    # Align existing status columns with the model's reserved enum width.
    for table, width in (("test_products", 9), ("test_rounds", 12)):
        with op.batch_alter_table(table) as batch:
            batch.alter_column("status", existing_type=sa.String(width), type_=sa.String(32), existing_nullable=False)
    op.add_column("test_cases", sa.Column("category", sa.String(32), nullable=False, server_default="basic"))
    op.add_column("test_points", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, test_round_id, sequence, status, created_at, details FROM test_round_events ORDER BY test_round_id, sequence")).mappings().all()
    for index, row in enumerate(rows):
        if row["status"] != "running" or index + 1 >= len(rows):
            continue
        following = rows[index + 1]
        if following["test_round_id"] != row["test_round_id"]:
            continue
        state = "succeeded" if following["status"] in ("running", "succeeded") else following["status"]
        details = json.loads(row["details"]) if isinstance(row["details"], str) else (row["details"] or {})
        details.update(finished_at=str(following["created_at"]), status_reconciled_from_sequence=following["sequence"])
        conn.execute(sa.text("UPDATE test_round_events SET status=:status, details=:details WHERE id=:id"), {"id":row["id"], "status":state, "details":json.dumps(details)})


def downgrade():
    for table, width in (("test_products", 9), ("test_rounds", 12)):
        with op.batch_alter_table(table) as batch:
            batch.alter_column("status", existing_type=sa.String(32), type_=sa.String(width), existing_nullable=False)
    op.drop_column("test_points", "submitted_at")
    op.drop_column("test_cases", "category")
    # Status corrections retain their evidence on downgrade.
