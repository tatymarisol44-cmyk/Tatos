"""Exact confirmed abonos and explicit financial deadlines; no bank connection."""

import sqlalchemy as sa
from alembic import op

revision = "0033_billing_ledger"
down_revision = "0032_marketing_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "billing_orders",
        sa.Column("credited_cents", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "billing_orders",
        sa.Column("payment_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("billing_orders", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "billing_movements",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("invoice_id", sa.String(32), nullable=False),
        sa.Column("merchant_id", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("bank_transaction_id", sa.String(120), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(200), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant", "request_id", name="uq_billing_movement_request"),
        sa.UniqueConstraint("merchant_id", "bank_transaction_id", name="uq_billing_movement_bank"),
    )
    op.create_index("ix_billing_movements_invoice", "billing_movements", ["tenant", "invoice_id"])


def downgrade() -> None:
    op.drop_table("billing_movements")
    for name in ("due_at", "payment_revision", "credited_cents"):
        op.drop_column("billing_orders", name)
