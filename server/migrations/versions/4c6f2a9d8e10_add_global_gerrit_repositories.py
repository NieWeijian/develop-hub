"""add global Gerrit repositories

Revision ID: 4c6f2a9d8e10
Revises: 2bd71a6e94c3
Create Date: 2026-09-11 15:15:00
"""

from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "4c6f2a9d8e10"
down_revision: Union[str, Sequence[str], None] = "2bd71a6e94c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gerrit_repositories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("project_path", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_path"),
    )
    op.create_index("ix_gerrit_repositories_name", "gerrit_repositories", ["name"])
    op.create_index("ix_gerrit_repositories_project_path", "gerrit_repositories", ["project_path"])

    with op.batch_alter_table("test_products") as batch_op:
        batch_op.add_column(sa.Column("gerrit_repository_id", sa.String(36), nullable=True))
        batch_op.create_index("ix_test_products_gerrit_repository_id", ["gerrit_repository_id"])
        batch_op.create_foreign_key(
            "fk_test_products_gerrit_repository_id",
            "gerrit_repositories",
            ["gerrit_repository_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    bind = op.get_bind()
    metadata = sa.MetaData()
    gerrit_repositories = sa.Table("gerrit_repositories", metadata, autoload_with=bind)
    test_products = sa.Table("test_products", metadata, autoload_with=bind)
    projects = [
        row[0]
        for row in bind.execute(
            sa.select(test_products.c.gerrit_project).distinct().where(test_products.c.gerrit_project != "")
        )
    ]
    for project_path in projects:
        repository_id = str(uuid4())
        bind.execute(
            gerrit_repositories.insert().values(
                id=repository_id,
                name=project_path.rsplit("/", 1)[-1],
                project_path=project_path,
                enabled=True,
                created_at=sa.func.now(),
                updated_at=sa.func.now(),
            )
        )
        bind.execute(
            test_products.update()
            .where(test_products.c.gerrit_project == project_path)
            .values(gerrit_repository_id=repository_id)
        )

    with op.batch_alter_table("test_products") as batch_op:
        batch_op.alter_column("gerrit_repository_id", existing_type=sa.String(36), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("test_products") as batch_op:
        batch_op.drop_constraint("fk_test_products_gerrit_repository_id", type_="foreignkey")
        batch_op.drop_index("ix_test_products_gerrit_repository_id")
        batch_op.drop_column("gerrit_repository_id")
    op.drop_table("gerrit_repositories")
