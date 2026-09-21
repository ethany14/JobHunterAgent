"""Keep interview assessments for each projected match report separately.

Revision ID: 0020_interview_match_cohorts
Revises: 0019_multi_agent_job_workflow
"""
from alembic import op
import sqlalchemy as sa

revision = "0020_interview_match_cohorts"
down_revision = "0019_multi_agent_job_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("application_requirement_assessments") as batch:
        batch.drop_constraint("uq_app_requirement_snapshot", type_="unique")
        batch.create_unique_constraint("uq_app_requirement_match", [
            "application_id", "snapshot_id", "source_match_artifact_id",
            "requirement_id",
        ])
    with op.batch_alter_table("interview_sessions") as batch:
        batch.add_column(sa.Column("source_match_artifact_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_interview_session_match_artifact",
            "application_artifacts", ["source_match_artifact_id"], ["artifact_id"])
    # Before this migration the old unique constraint allowed only one cohort
    # per application/snapshot. Bind existing interviews to that original cohort.
    op.execute(sa.text("""
        UPDATE interview_sessions
        SET source_match_artifact_id = (
            SELECT a.source_match_artifact_id
            FROM application_requirement_assessments AS a
            WHERE a.application_id = interview_sessions.application_id
              AND a.snapshot_id = interview_sessions.snapshot_id
            LIMIT 1
        )
    """))


def downgrade() -> None:
    raise RuntimeError("Interview match cohorts migration is forward-only.")
