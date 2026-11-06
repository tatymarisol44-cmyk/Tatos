"""Guarded gateway reconciliation and automatic WhatsApp utility reminders.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "billing_gateway_renewals", sa.Column("provider_created_at", sa.String(48), nullable=True)
    )
    op.create_table(
        "billing_auto_policies",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("account_id", sa.String(32), nullable=True),
        sa.Column("template_name", sa.String(128), nullable=False),
        sa.Column("language", sa.String(12), nullable=False),
        sa.Column("cadence_days", sa.Integer(), nullable=False),
        sa.Column("maximum", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "billing_auto_targets",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("source_key", sa.String(160), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("last_result", sa.String(48), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "billing_auto_attempts",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("attempt_id", sa.String(32), primary_key=True),
        sa.Column("source_key", sa.String(160), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("provider_message_id", sa.String(256), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("sender_tenant", sa.String(64), nullable=False),
        sa.Column("contact_day", sa.String(10), nullable=False),
        sa.Column("address_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant", "source_key", "ordinal", name="uq_billing_auto_attempt"),
        sa.UniqueConstraint(
            "sender_tenant", "address_key", "contact_day", name="uq_billing_auto_daily"
        ),
    )


def downgrade() -> None:
    op.drop_table("billing_auto_attempts")
    op.drop_table("billing_auto_targets")
    op.drop_table("billing_auto_policies")
    op.drop_column("billing_gateway_renewals", "provider_created_at")
