"""Browser-bound Meta login and encrypted account grants.

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
        "social_credentials",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("secret_ref", sa.String(64), primary_key=True),
        sa.Column("network", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("token_sealed", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("network", "external_id", name="uq_social_oauth_asset"),
    )
    op.create_table(
        "social_login_flows",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("flow_id", sa.String(32), primary_key=True, unique=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("network", sa.String(16), nullable=False),
        sa.Column("professional_id", sa.String(64), nullable=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payload_sealed", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("account_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("social_login_flows")
    op.drop_table("social_credentials")
