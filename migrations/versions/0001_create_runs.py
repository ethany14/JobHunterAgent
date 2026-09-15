"""Create persisted API runs table.

Revision ID: 0001_create_runs
Revises: None
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_create_runs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("resume_text", sa.Text(), nullable=False),
        sa.Column("job_description", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'awaiting_review', 'revising', 'approved', 'failed')",
            name="ck_runs_status",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("thread_id"),
    )
    op.create_index("ix_runs_status", "runs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_table("runs")
