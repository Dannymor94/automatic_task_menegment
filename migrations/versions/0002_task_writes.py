"""task_writes: маркер идемпотентности записи в YouGile

Revision ID: 0002_task_writes
Revises: 0001_initial
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_task_writes"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_writes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("meeting_id", sa.Integer(), sa.ForeignKey("meetings.id"), nullable=False),
        sa.Column("internal_task_id", sa.String(length=120), nullable=False),
        sa.Column("yougile_task_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.UniqueConstraint("internal_task_id", name="uq_task_writes_internal"),
    )


def downgrade() -> None:
    op.drop_table("task_writes")
