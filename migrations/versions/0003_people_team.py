"""people: specialty + project (база команды для UI и контекста промпта)

Revision ID: 0003_people_team
Revises: 0002_task_writes
Create Date: 2026-06-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_people_team"
down_revision = "0002_task_writes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("people") as batch:
        batch.add_column(sa.Column("specialty", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("project", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("people") as batch:
        batch.drop_column("project")
        batch.drop_column("specialty")
