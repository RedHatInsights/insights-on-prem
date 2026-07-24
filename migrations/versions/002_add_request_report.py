"""add request_report table

Revision ID: 002
Revises: 001
Create Date: 2026-04-01 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_report",
        sa.Column("request_id", sa.VARCHAR(), nullable=False),
        sa.Column("cluster_id", sa.VARCHAR(), nullable=False),
        sa.Column("report", sa.VARCHAR(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("request_id", name="request_report_pkey"),
    )

    op.create_index("idx_request_report_created_at", "request_report", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_request_report_created_at", table_name="request_report")
    op.drop_table("request_report")
