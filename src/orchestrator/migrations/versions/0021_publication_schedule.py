"""publication calendar: a publication may be scheduled for a date and time

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-14 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("publications") as batch:
        batch.add_column(sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        op.f("ix_publications_scheduled_for"), "publications", ["scheduled_for"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_publications_scheduled_for"), table_name="publications")
    with op.batch_alter_table("publications") as batch:
        batch.drop_column("scheduled_for")
