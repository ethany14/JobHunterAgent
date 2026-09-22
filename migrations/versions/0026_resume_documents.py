"""Add server-side PDF resume documents.

Revision ID: 0026_resume_documents
Revises: 0025_conversation_learning
"""

from alembic import op
import sqlalchemy as sa

revision = "0026_resume_documents"
down_revision = "0025_conversation_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resume_documents",
        sa.Column("resume_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "content_sha256", name="uq_resume_owner_content"),
    )
    op.create_index("ix_resume_documents_owner_id", "resume_documents", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_resume_documents_owner_id", table_name="resume_documents")
    op.drop_table("resume_documents")
