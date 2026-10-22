"""Patient accounts and expiring sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "patient_logins",
        sa.Column("tenant", sa.String(64), nullable=False, primary_key=True),
        sa.Column("patient_id", sa.String(64), nullable=False, primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, primary_key=False),
        sa.Column("password_hash", sa.String(200), nullable=False, primary_key=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True, primary_key=False),
        sa.Column("recovery_hash", sa.String(64), nullable=True, primary_key=False),
    )
    op.create_index("ix_patient_logins_email", "patient_logins", ["email"], unique=True)
    op.create_table(
        "patient_sessions",
        sa.Column("tenant", sa.String(64), nullable=False, primary_key=True),
        sa.Column("key_hash", sa.String(64), nullable=False, primary_key=True),
        sa.Column("patient_id", sa.String(64), nullable=False, primary_key=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, primary_key=False),
        sa.UniqueConstraint("key_hash", name="uq_patient_sessions_key_hash"),
    )
    op.create_index(
        "ix_patient_sessions_expires_at", "patient_sessions", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_table("patient_sessions")
    op.drop_table("patient_logins")
