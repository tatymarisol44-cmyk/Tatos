"""Manual audience groups and a frozen group selection on each campaign."""

import sqlalchemy as sa
from alembic import op

revision = "0029_marketing_groups"
down_revision = "0028_image_scoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "marketing_groups",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant", "name_key", name="uq_marketing_group_name"),
    )
    op.create_table(
        "marketing_group_members",
        sa.Column("tenant", sa.String(64), primary_key=True),
        sa.Column("group_id", sa.String(64), primary_key=True),
        sa.Column("patient_id", sa.String(64), primary_key=True),
        sa.UniqueConstraint("tenant", "patient_id", name="uq_marketing_person_one_group"),
        sa.ForeignKeyConstraint(
            ["tenant", "group_id"],
            ["marketing_groups.tenant", "marketing_groups.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant", "patient_id"], ["crm_patients.tenant", "crm_patients.id"], ondelete="CASCADE"
        ),
    )
    op.add_column("campaigns", sa.Column("audience_groups", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("campaigns", "audience_groups")
    op.drop_table("marketing_group_members")
    op.drop_table("marketing_groups")
