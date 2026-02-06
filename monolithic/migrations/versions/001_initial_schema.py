"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-01-21 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create report table
    op.create_table(
        'report',
        sa.Column('cluster', sa.VARCHAR(), nullable=False),
        sa.Column('report', sa.VARCHAR(), nullable=False),
        sa.Column('reported_at', sa.DateTime(), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('gathered_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('cluster', name='report_pkey')
    )

    # Create indexes for report table
    op.create_index('idx_report_last_checked', 'report', ['last_checked_at'])

    # Create rule_hit table
    op.create_table(
        'rule_hit',
        sa.Column('cluster_id', sa.VARCHAR(), nullable=False),
        sa.Column('rule_fqdn', sa.VARCHAR(), nullable=False),
        sa.Column('error_key', sa.VARCHAR(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('cluster_id', 'rule_fqdn', 'error_key', name='rule_hit_pkey')
    )

    # Create indexes for rule_hit table
    op.create_index('idx_rule_hit_cluster', 'rule_hit', ['cluster_id'])
    op.create_index('idx_rule_hit_rule_fqdn', 'rule_hit', ['rule_fqdn'])

    # Create report_info table
    op.create_table(
        'report_info',
        sa.Column('cluster_id', sa.VARCHAR(), nullable=False),
        sa.Column('version_info', sa.VARCHAR(), nullable=False),
        sa.PrimaryKeyConstraint('cluster_id', name='report_info_pkey')
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index('idx_rule_hit_rule_fqdn', table_name='rule_hit')
    op.drop_index('idx_rule_hit_cluster', table_name='rule_hit')
    op.drop_index('idx_report_last_checked', table_name='report')

    # Drop tables
    op.drop_table('report_info')
    op.drop_table('rule_hit')
    op.drop_table('report')
