"""initial schema: projects, people, meetings

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("yougile_project_id", sa.String(length=100), nullable=True),
        sa.Column("yougile_board_id", sa.String(length=100), nullable=True),
        sa.Column("yougile_column_id", sa.String(length=100), nullable=True),
        sa.UniqueConstraint("name", name="uq_projects_name"),
    )
    op.create_table(
        "people",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("yougile_user_id", sa.String(length=100), nullable=True),
        sa.Column("default_role", sa.String(length=50), nullable=True),
        sa.UniqueConstraint("name", name="uq_people_name"),
    )
    op.create_table(
        "meetings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("source_file", sa.String(length=500), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("meetings")
    op.drop_table("people")
    op.drop_table("projects")
