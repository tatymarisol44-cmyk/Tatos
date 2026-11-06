"""Preserve professional criteria and administration data alongside each report."""

import sqlalchemy as sa
from alembic import op

revision = "0027_test_parameters"
down_revision = "0026_test_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("test_image_reports", sa.Column("parameters", sa.JSON(), nullable=True))
    op.add_column("test_image_reports", sa.Column("application", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("test_image_reports", "application")
    op.drop_column("test_image_reports", "parameters")
