"""Sealed dLocal Go connections and durable checkout/renewal reconciliation.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_gateway_accounts",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("account_id", sa.String(32), nullable=False, unique=True),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("key_fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("credentials_sealed", sa.Text(), nullable=False),
        sa.Column("withdrawals_confirmed", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "billing_gateway_checkouts",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("invoice_id", sa.String(32), primary_key=True),
        sa.Column("reference", sa.String(32), nullable=False, unique=True),
        sa.Column("account_id", sa.String(32), nullable=False),
        sa.Column("payment_id", sa.String(128), nullable=True),
        sa.Column("url_sealed", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "payment_id", name="uq_gateway_payment"),
    )
    op.create_table(
        "billing_gateway_plans",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("reference", sa.String(32), nullable=False, unique=True),
        sa.Column("account_id", sa.String(32), nullable=False),
        sa.Column("plan_id", sa.String(128), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("url_sealed", sa.Text(), nullable=True),
        sa.Column("consented_by", sa.String(64), nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "plan_id", name="uq_gateway_clinic_plan"),
    )
    op.create_table(
        "billing_gateway_renewals",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("execution_id", sa.String(128), primary_key=True),
        sa.Column("subscription_id", sa.String(128), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("billing_gateway_renewals")
    op.drop_table("billing_gateway_plans")
    op.drop_table("billing_gateway_checkouts")
    op.drop_table("billing_gateway_accounts")
