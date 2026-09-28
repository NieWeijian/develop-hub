"""add versioned QA plugins

Revision ID: d12b77e48f91
Revises: c94518f2b7a4
Create Date: 2026-09-02 11:20:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d12b77e48f91"
down_revision: Union[str, Sequence[str], None] = "c94518f2b7a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "qa_plugin_releases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("coding_run_id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=80), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source_relpath", sa.Text(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("bundle_path", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "INSTALLING", "INSTALLED", "FAILED", name="qapluginstatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("remote_path", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["coding_run_id"], ["coding_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plugin_id", "version", name="uq_qa_plugin_release_version"),
    )
    op.create_index(op.f("ix_qa_plugin_releases_coding_run_id"), "qa_plugin_releases", ["coding_run_id"])
    op.create_index(op.f("ix_qa_plugin_releases_created_at"), "qa_plugin_releases", ["created_at"])
    op.create_index(op.f("ix_qa_plugin_releases_plugin_id"), "qa_plugin_releases", ["plugin_id"])
    op.create_index(op.f("ix_qa_plugin_releases_sha256"), "qa_plugin_releases", ["sha256"])
    op.create_index(op.f("ix_qa_plugin_releases_status"), "qa_plugin_releases", ["status"])

    op.drop_index(op.f("ix_test_cases_code"), table_name="test_cases")
    op.create_index(op.f("ix_test_cases_code"), "test_cases", ["code"], unique=False)
    with op.batch_alter_table("test_cases") as batch_op:
        batch_op.add_column(sa.Column("plugin_release_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("plugin_id", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("plugin_version", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            "fk_test_cases_plugin_release_id",
            "qa_plugin_releases",
            ["plugin_release_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(op.f("ix_test_cases_plugin_release_id"), ["plugin_release_id"], unique=False)

    with op.batch_alter_table("qa_case_executions") as batch_op:
        batch_op.add_column(sa.Column("plugin_release_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("plugin_id", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("plugin_version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("qa_case_executions") as batch_op:
        batch_op.drop_column("plugin_version")
        batch_op.drop_column("plugin_id")
        batch_op.drop_column("plugin_release_id")
    with op.batch_alter_table("test_cases") as batch_op:
        batch_op.drop_index(op.f("ix_test_cases_plugin_release_id"))
        batch_op.drop_constraint("fk_test_cases_plugin_release_id", type_="foreignkey")
        batch_op.drop_column("plugin_version")
        batch_op.drop_column("plugin_id")
        batch_op.drop_column("plugin_release_id")
    op.drop_index(op.f("ix_qa_plugin_releases_status"), table_name="qa_plugin_releases")
    op.drop_index(op.f("ix_qa_plugin_releases_sha256"), table_name="qa_plugin_releases")
    op.drop_index(op.f("ix_qa_plugin_releases_plugin_id"), table_name="qa_plugin_releases")
    op.drop_index(op.f("ix_qa_plugin_releases_created_at"), table_name="qa_plugin_releases")
    op.drop_index(op.f("ix_qa_plugin_releases_coding_run_id"), table_name="qa_plugin_releases")
    op.drop_table("qa_plugin_releases")
