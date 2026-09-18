"""Add the distinct approved Skill lifecycle state.

Revision ID: 0012_context_management
Revises: 0011_context_runtime
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_context_management"
down_revision = "0011_context_runtime"
branch_labels = None
depends_on = None


OLD_STATUS = (
    "status IN ('draft','validating','validated','approval_required','active',"
    "'rejected','superseded','retired')"
)
NEW_STATUS = (
    "status IN ('draft','validating','validated','approval_required','approved','active',"
    "'rejected','superseded','retired')"
)


def upgrade() -> None:
    with op.batch_alter_table("skill_versions") as batch:
        batch.drop_constraint("ck_skill_versions_status", type_="check")
        batch.create_check_constraint("ck_skill_versions_status", NEW_STATUS)
    op.create_table(
        "skill_evaluation_results",
        sa.Column("evaluation_id", sa.String(36), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(36),
            sa.ForeignKey("skill_versions.version_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("suite_name", sa.String(128), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_skill_evaluation_results_version_id",
        "skill_evaluation_results",
        ["version_id"],
    )


def downgrade() -> None:
    # An approved version has no exact equivalent in the old lifecycle. Moving
    # it back to approval_required retains the requirement for a human action.
    op.drop_index(
        "ix_skill_evaluation_results_version_id",
        table_name="skill_evaluation_results",
    )
    op.drop_table("skill_evaluation_results")
    op.execute(
        sa.text(
            "UPDATE skill_versions SET status = 'approval_required' "
            "WHERE status = 'approved'"
        )
    )
    with op.batch_alter_table("skill_versions") as batch:
        batch.drop_constraint("ck_skill_versions_status", type_="check")
        batch.create_check_constraint("ck_skill_versions_status", OLD_STATUS)
