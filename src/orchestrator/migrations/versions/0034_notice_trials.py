"""Private-only staged notice scenarios; no live sender."""

import sqlalchemy as sa
from alembic import op

revision = "0034_notice_trials"
down_revision = "0033_billing_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_notice_policies",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
    )
    op.create_table(
        "billing_notice_trials",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("invoice_id", sa.String(32), nullable=False),
        sa.Column("event_key", sa.String(100), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("text", sa.String(1000), nullable=False),
        sa.Column("simulated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant", "invoice_id", "event_key", name="uq_billing_notice_trial"),
    )


def downgrade() -> None:
    op.drop_table("billing_notice_trials")
    op.drop_table("billing_notice_policies")
