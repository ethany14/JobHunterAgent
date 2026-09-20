"""Add safe Workspace analysis failure state.

Revision ID: 0014_workspace_failure
Revises: 0013_job_workspace
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_workspace_failure"
down_revision = "0013_job_workspace"
branch_labels = None
depends_on = None

OLD = "status IN ('saved','analyzing','needs_evidence','materials_ready','ready_to_apply','applied','interviewing','offer','rejected','withdrawn','archived')"
NEW = "status IN ('saved','analyzing','analysis_failed','needs_evidence','materials_ready','ready_to_apply','applied','interviewing','offer','rejected','withdrawn','archived')"


def upgrade() -> None:
    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint("ck_applications_status", type_="check")
        batch.create_check_constraint("ck_applications_status", NEW)
        batch.add_column(sa.Column("error_code", sa.String(64)))


def downgrade() -> None:
    op.execute(sa.text("UPDATE applications SET status='archived' WHERE status='analysis_failed'"))
    with op.batch_alter_table("applications") as batch:
        batch.drop_column("error_code")
        batch.drop_constraint("ck_applications_status", type_="check")
        batch.create_check_constraint("ck_applications_status", OLD)
