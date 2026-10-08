"""clinical files (signed consents, external reports, scanned tests) in the database

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-08 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "clinical_files",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("file_id", sa.String(length=32), nullable=False),
        sa.Column("patient_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("filename", sa.String(length=200), nullable=False),
        sa.Column("media_type", sa.String(length=40), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("access", sa.String(length=24), nullable=False),
        sa.Column("author_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "file_id"),
    )
    op.create_index(
        op.f("ix_clinical_files_patient_id"), "clinical_files", ["patient_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_clinical_files_patient_id"), table_name="clinical_files")
    op.drop_table("clinical_files")
