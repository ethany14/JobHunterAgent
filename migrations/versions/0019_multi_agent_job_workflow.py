"""Persist explicit Standard versus Multi-Agent ownership.

Revision ID: 0019_multi_agent_job_workflow
Revises: 0018_multi_agent_runtime
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_multi_agent_job_workflow"
down_revision = "0018_multi_agent_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing records are historical Standard executions. New records pass an
    # explicit mode; the database default keeps old writers safe during rollout.
    for table in ("application_packs", "application_artifacts", "agent_plans", "agent_task_artifacts"):
        op.add_column(table, sa.Column("workflow_mode", sa.String(32), nullable=False,
                                      server_default="single_custom"))


def downgrade() -> None:
    raise RuntimeError("Workflow ownership migration is forward-only.")
