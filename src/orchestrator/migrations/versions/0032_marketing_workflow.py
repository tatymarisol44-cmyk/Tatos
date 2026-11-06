"""Tenant branding and persisted existing-agent marketing workflows."""

import sqlalchemy as sa
from alembic import op

revision = "0032_marketing_workflow"
down_revision = "0031_availability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "marketing_brands",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("logo", sa.LargeBinary(), nullable=True),
        sa.Column("logo_sha256", sa.String(64), nullable=True),
    )
    op.create_table(
        "marketing_workflows",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("marketing_workflows")
    op.drop_table("marketing_brands")
