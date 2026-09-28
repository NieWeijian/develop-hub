"""add product-driven test workbench

Revision ID: 19a7c4d8e2f0
Revises: f905c62d1a48
Create Date: 2026-09-10 11:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "19a7c4d8e2f0"
down_revision: Union[str, Sequence[str], None] = "f905c62d1a48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sdk_repositories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("board_configs", sa.JSON(), nullable=False),
        sa.Column("artifact_root", sa.Text(), nullable=False),
        sa.Column("artifact_patterns", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("root_path"),
    )
    op.create_index("ix_sdk_repositories_name", "sdk_repositories", ["name"])
    op.create_table(
        "test_products",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sdk_repository_id", sa.String(36), sa.ForeignKey("sdk_repositories.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("tested_repository_id", sa.String(36), sa.ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("gerrit_project", sa.String(255), nullable=False),
        sa.Column("board_config", sa.String(160), nullable=False),
        sa.Column("kernel_baseline", sa.String(120), nullable=False),
        sa.Column("uboot_baseline", sa.String(120), nullable=False),
        sa.Column("rkbin_baseline", sa.String(120), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("manifest_snapshot", sa.JSON(), nullable=False),
        sa.Column("repository_snapshot", sa.JSON(), nullable=False),
        sa.Column("baseline_result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", name="uq_test_products_code"),
        sa.UniqueConstraint("sdk_repository_id", "tested_repository_id", name="uq_test_product_sdk_repo"),
    )
    for column in ("code", "sdk_repository_id", "tested_repository_id", "gerrit_project", "status", "created_at"):
        op.create_index(f"ix_test_products_{column}", "test_products", [column])
    op.create_table(
        "test_points",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("test_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("change_number", sa.Integer(), nullable=False),
        sa.Column("patchset_number", sa.Integer(), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(400), nullable=False),
        sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("owner", sa.String(160), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("product_id", "change_number", "patchset_number", name="uq_test_point_patchset"),
    )
    for column in ("product_id", "commit_sha", "ready"):
        op.create_index(f"ix_test_points_{column}", "test_points", [column])
    op.create_table(
        "test_benches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("user", sa.String(120), nullable=False),
        sa.Column("remote_root", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_test_benches_name", "test_benches", ["name"])
    op.create_table(
        "test_rounds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("display_number", sa.Integer(), nullable=False, unique=True),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("test_products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("test_point_id", sa.String(36), sa.ForeignKey("test_points.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("bench_id", sa.String(36), sa.ForeignKey("test_benches.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("product_snapshot", sa.JSON(), nullable=False),
        sa.Column("manifest_commit", sa.String(64), nullable=False),
        sa.Column("board_config", sa.String(160), nullable=False),
        sa.Column("gerrit_project", sa.String(255), nullable=False),
        sa.Column("change_number", sa.Integer(), nullable=False),
        sa.Column("patchset_number", sa.Integer(), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("work_branch", sa.String(255), nullable=False),
        sa.Column("build_result", sa.JSON(), nullable=True),
        sa.Column("image_snapshot", sa.JSON(), nullable=False),
        sa.Column("test_case_snapshot", sa.JSON(), nullable=False),
        sa.Column("generated_config", sa.JSON(), nullable=True),
        sa.Column("remote_path", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ("display_number", "product_id", "test_point_id", "bench_id", "status", "created_at"):
        op.create_index(f"ix_test_rounds_{column}", "test_rounds", [column])
    op.create_table(
        "test_round_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("test_round_id", sa.String(36), sa.ForeignKey("test_rounds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("test_round_id", "sequence", name="uq_test_round_event_sequence"),
    )
    for column in ("test_round_id", "event_type", "created_at"):
        op.create_index(f"ix_test_round_events_{column}", "test_round_events", [column])


def downgrade() -> None:
    op.drop_table("test_round_events")
    op.drop_table("test_rounds")
    op.drop_table("test_benches")
    op.drop_table("test_points")
    op.drop_table("test_products")
    op.drop_table("sdk_repositories")
