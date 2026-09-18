"""Add persisted backend ownership for controlled custom-runtime cutover.

Revision ID: 0004_backend_cutover
Revises: 0003_tool_runtime
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_backend_cutover"
down_revision = "0003_tool_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("backend", sa.String(16), nullable=False,
                                   server_default="unknown"))
        batch.add_column(sa.Column("backend_source", sa.String(32), nullable=False,
                                   server_default="unclassified"))
        batch.create_check_constraint(
            "ck_runs_backend", "backend IN ('custom', 'langgraph', 'unknown')"
        )
        batch.create_index("ix_runs_backend", ["backend"], unique=False)

    # Custom state is strong same-database evidence. Remaining rows stay
    # explicitly unknown until the checkpoint-aware classifier runs.
    op.execute(sa.text("""
        UPDATE runs
        SET backend = 'custom', backend_source = 'custom_state'
        WHERE EXISTS (
            SELECT 1 FROM custom_agent_states
            WHERE custom_agent_states.run_id = runs.run_id
        )
    """))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_backend")
        batch.drop_constraint("ck_runs_backend", type_="check")
        batch.drop_column("backend_source")
        batch.drop_column("backend")
