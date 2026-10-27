"""Separate clinic receivables from platform subscription invoices.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_payees",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("merchant_id", sa.String(64), nullable=False, unique=True),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "billing_orders",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("invoice_id", sa.String(32), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("patient_id", sa.String(64), nullable=True),
        sa.Column("merchant_id", sa.String(64), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("bank_instructions", sa.Text(), nullable=False),
        sa.Column("transfer_reference", sa.String(120), nullable=True),
        sa.Column("review_note", sa.String(200), nullable=True),
        sa.Column("settlement_id", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant", "kind", "resource_id", name="uq_billing_receivable"),
        sa.UniqueConstraint("merchant_id", "settlement_id", name="uq_billing_bank_settlement"),
    )


def downgrade() -> None:
    op.drop_table("billing_orders")
    op.drop_table("billing_payees")
