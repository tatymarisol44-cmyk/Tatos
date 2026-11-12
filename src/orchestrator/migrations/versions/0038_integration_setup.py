"""Sealed, tenant-owned onboarding references; no external credentials or transport."""

import sqlalchemy as sa
from alembic import op

revision = "0038_integration_setup"
down_revision = "0037_tax_trials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_setup_profiles",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(24), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("profile_sealed", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("integration_setup_profiles")
