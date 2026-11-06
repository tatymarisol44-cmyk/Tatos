"""Non-fiscal private multi-issuer prototype, never a production invoice ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0037_tax_trials"
down_revision = "0036_instrument_imports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tax_trial_issuers",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("ruc", sa.String(13), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("profile", sa.JSON(), nullable=False),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("tenant", "ruc", name="uq_tax_trial_ruc"),
    )
    op.create_table(
        "tax_trial_documents",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("issuer_id", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(32), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("xml", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("outcome", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant", "request_id", name="uq_tax_trial_request"),
        sa.UniqueConstraint("tenant", "issuer_id", "sequence", name="uq_tax_trial_sequence"),
        sa.UniqueConstraint("tenant", "issuer_id", "order_id", name="uq_tax_trial_order"),
    )
    op.create_index("ix_tax_trial_created", "tax_trial_documents", ["tenant", "created_at"])


def downgrade() -> None:
    op.drop_table("tax_trial_documents")
    op.drop_table("tax_trial_issuers")
