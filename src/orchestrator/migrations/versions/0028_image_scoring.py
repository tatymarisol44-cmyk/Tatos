"""Pinned scoring definitions and professionally verified answers from test images."""

import sqlalchemy as sa
from alembic import op

revision = "0028_image_scoring"
down_revision = "0027_test_parameters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("test_image_reports", sa.Column("score_data", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("test_image_reports", "score_data")
