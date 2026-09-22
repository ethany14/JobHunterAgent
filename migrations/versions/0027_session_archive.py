"""Add local conversation archive projection.

Revision ID: 0027_session_archive
Revises: 0026_resume_documents
"""

from alembic import op
import sqlalchemy as sa

revision = "0027_session_archive"
down_revision = "0026_resume_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_sessions_archived_at", "agent_sessions", ["archived_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_archived_at", table_name="agent_sessions")
    op.drop_column("agent_sessions", "archived_at")
