"""Voluntary, versioned service surveys with tenant-local invitations and responses."""

import sqlalchemy as sa
from alembic import op

revision = "0035_surveys"
down_revision = "0034_notice_trials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "survey_definitions",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "survey_settings",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
    )
    op.create_table(
        "survey_invitations",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("patient_id", sa.String(64), nullable=False),
        sa.Column("appointment_id", sa.String(64), nullable=False),
        sa.Column("survey_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_reminder_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reminder_count", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=True),
        sa.Column("satisfaction", sa.Integer(), nullable=True),
        sa.Column("critical", sa.Boolean(), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant", "appointment_id", "survey_id", "version", name="uq_survey_invitation"
        ),
    )
    op.create_table(
        "survey_events",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("invitation_id", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant", "invitation_id", "channel", "ordinal", name="uq_survey_event"
        ),
    )
    op.create_index(
        "ix_surveys_patient_pending",
        "survey_invitations",
        ["tenant", "patient_id", "state", "due_at"],
    )
    op.create_index("ix_surveys_reminders", "survey_invitations", ["state", "next_reminder_at"])
    op.create_index("ix_survey_events_tenant_created", "survey_events", ["tenant", "created_at"])


def downgrade() -> None:
    for name in ("survey_events", "survey_invitations", "survey_settings", "survey_definitions"):
        op.drop_table(name)
