"""canonical knowledge text in the database (Qdrant becomes a rebuildable index)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema. Nullable: rows uploaded before this revision have no stored text
    (`agency knowledge reindex` lists them so they can be uploaded again)."""
    op.add_column("knowledge_documents", sa.Column("text", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("knowledge_documents", "text")
