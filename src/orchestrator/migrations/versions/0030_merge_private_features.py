"""Join existing payment and image/marketing migration histories without rewriting them."""

revision = "0030_private_integration"
down_revision = ("0029", "0029_marketing_groups")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
