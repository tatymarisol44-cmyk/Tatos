"""Single-source availability, date exceptions and immutable booking duration/buffer."""

import sqlalchemy as sa
from alembic import op

revision = "0031_availability"
down_revision = "0030_private_integration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "professionals",
        sa.Column("schedule_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "professional_hours",
        sa.Column("break_minutes", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "crm_appointments",
        sa.Column("buffer_minutes", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("crm_appointments", sa.Column("service_id", sa.String(32), nullable=True))
    op.add_column(
        "crm_appointments",
        sa.Column("consultation_mode", sa.String(16), nullable=False, server_default="in_person"),
    )
    op.create_table(
        "schedule_settings",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("professional_id", sa.String(64), primary_key=True),
        sa.Column("settings", sa.JSON(), nullable=False),
    )
    op.create_table(
        "schedule_exceptions",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("professional_id", sa.String(64), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("blocks", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("schedule_exceptions")
    op.drop_table("schedule_settings")
    for name in ["consultation_mode", "service_id", "buffer_minutes"]:
        op.drop_column("crm_appointments", name)
    op.drop_column("professional_hours", "break_minutes")
    op.drop_column("professionals", "schedule_version")
