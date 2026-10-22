"""Clinician-recorded clinical measurements.

Revision ID: 0026
Revises: 0025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clinical_metrics",
        sa.Column("tenant", sa.String(length=64), nullable=False),
        sa.Column("metric_id", sa.String(length=32), nullable=False),
        sa.Column("patient_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=40), nullable=False),
        sa.Column("measured_on", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("author_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant", "metric_id"),
    )
    op.create_index(
        "ix_clinical_metrics_tenant_patient_date",
        "clinical_metrics",
        ["tenant", "patient_id", "measured_on"],
    )


def downgrade() -> None:
    op.drop_index("ix_clinical_metrics_tenant_patient_date", table_name="clinical_metrics")
    op.drop_table("clinical_metrics")
