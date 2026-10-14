"""inbox: WhatsApp, Instagram and Facebook conversations, messages sealed at rest

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-14 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "inbox_conversations",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=32), nullable=False),
        sa.Column("network", sa.String(length=16), nullable=False),
        sa.Column("account_id", sa.String(length=32), nullable=False),
        sa.Column("address_key", sa.String(length=64), nullable=False),
        sa.Column("address_sealed", sa.Text(), nullable=False),
        sa.Column("patient_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attention", sa.String(length=16), nullable=True),
        sa.Column("unread", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "conversation_id"),
    )
    op.create_index(
        "ux_inbox_conversations_who",
        "inbox_conversations",
        ["tenant", "network", "account_id", "address_key"],
        unique=True,
    )
    op.create_table(
        "inbox_messages",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("body_sealed", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(length=16), nullable=True),
        sa.Column("author", sa.String(length=128), nullable=True),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "message_id"),
    )
    op.create_index(
        "ix_inbox_messages_conversation",
        "inbox_messages",
        ["tenant", "conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_inbox_messages_conversation", table_name="inbox_messages")
    op.drop_table("inbox_messages")
    op.drop_index("ux_inbox_conversations_who", table_name="inbox_conversations")
    op.drop_table("inbox_conversations")
