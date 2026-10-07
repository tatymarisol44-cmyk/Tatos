"""append-only clinical documents written by professionals

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "clinical_documents",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=32), nullable=False),
        sa.Column("patient_id", sa.String(length=64), nullable=False),
        sa.Column("doc_type", sa.String(length=60), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("access", sa.String(length=24), nullable=False),
        sa.Column("author_id", sa.String(length=128), nullable=False),
        sa.Column("professional_id", sa.String(length=64), nullable=True),
        sa.Column("pack_id", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("amends", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "document_id"),
    )
    op.create_index(
        op.f("ix_clinical_documents_patient_id"),
        "clinical_documents",
        ["patient_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_clinical_documents_patient_id"), table_name="clinical_documents")
    op.drop_table("clinical_documents")
