"""Staff sign-in with e-mail, password and an e-mailed code (ADR 0024)

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-15 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "staff_logins",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=200), nullable=True),
        sa.Column("password_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sign_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "principal_id"),
    )
    op.create_index(op.f("ix_staff_logins_email"), "staff_logins", ["email"], unique=True)
    op.create_table(
        "login_challenges",
        sa.Column("challenge_id", sa.String(length=32), nullable=False),
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("sends", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("challenge_id"),
    )
    op.create_index(op.f("ix_login_challenges_principal_id"), "login_challenges", ["principal_id"])
    op.create_index(op.f("ix_login_challenges_secret_hash"), "login_challenges", ["secret_hash"])
    op.create_table(
        "staff_sessions",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_index(op.f("ix_staff_sessions_principal_id"), "staff_sessions", ["principal_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_staff_sessions_principal_id"), table_name="staff_sessions")
    op.drop_table("staff_sessions")
    op.drop_index(op.f("ix_login_challenges_secret_hash"), table_name="login_challenges")
    op.drop_index(op.f("ix_login_challenges_principal_id"), table_name="login_challenges")
    op.drop_table("login_challenges")
    op.drop_index(op.f("ix_staff_logins_email"), table_name="staff_logins")
    op.drop_table("staff_logins")
