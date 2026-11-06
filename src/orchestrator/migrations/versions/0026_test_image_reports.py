"""Author-only visual test drafts. No analysis or activation during migration."""

import sqlalchemy as sa
from alembic import op

revision = "0026_test_images"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "test_image_reports",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("report_id", sa.String(32), primary_key=True),
        sa.Column("patient_id", sa.String(64), nullable=False, index=True),
        sa.Column("file_id", sa.String(32), nullable=False),
        sa.Column("author_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("extraction", sa.JSON(), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("test_image_reports")
