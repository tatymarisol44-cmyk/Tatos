"""Private licensed-source drafts with atomic instrument incorporation."""

import sqlalchemy as sa
from alembic import op

revision = "0036_instrument_imports"
down_revision = "0035_surveys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instrument_imports",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("author_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("format", sa.String(20), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("instrument_id", sa.String(40), nullable=True),
        sa.Column("review", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_instrument_imports_author", "instrument_imports", ["tenant", "author_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("instrument_imports")
