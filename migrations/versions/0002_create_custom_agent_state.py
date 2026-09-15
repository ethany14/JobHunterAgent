"""Create custom agent state and event tables.

Revision ID: 0002_custom_agent
Revises: 0001_create_runs
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_custom_agent"
down_revision = "0001_create_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "custom_agent_states",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("step", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "custom_agent_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("step", sa.String(length=48), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_custom_event_sequence"),
    )
    op.create_index(
        "ix_custom_agent_events_run_id",
        "custom_agent_events",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_custom_agent_events_run_id", table_name="custom_agent_events")
    op.drop_table("custom_agent_events")
    op.drop_table("custom_agent_states")
