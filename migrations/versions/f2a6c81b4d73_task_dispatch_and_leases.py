"""task dispatch rows and execution leases

Revision ID: f2a6c81b4d73
Revises: d945a30dca4a
Create Date: 2026-10-03 11:09:30.114280
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2a6c81b4d73'
down_revision: Union[str, None] = 'd945a30dca4a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'job_dispatches',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('tenant_id', sa.String(length=32), nullable=False),
        sa.Column('job_id', sa.String(length=32), nullable=False),
        sa.Column('attempt_no', sa.Integer(), nullable=False),
        sa.Column('queue_name', sa.String(length=120), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('publish_confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('publish_attempts', sa.Integer(), nullable=False),
        sa.Column('lease_token', sa.String(length=64), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['job_id'], ['processing_jobs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id', 'attempt_no', name='uq_job_dispatch_attempt'),
    )
    op.create_index('ix_job_dispatches_job_id', 'job_dispatches', ['job_id'], unique=False)
    op.create_index('ix_job_dispatches_status', 'job_dispatches', ['status'], unique=False)
    op.create_index(
        'ix_job_dispatches_tenant_id', 'job_dispatches', ['tenant_id'], unique=False
    )
    op.create_index(
        'ix_job_dispatches_status_created', 'job_dispatches', ['status', 'created_at'], unique=False
    )
    op.create_index(
        'ix_job_dispatches_lease_expires', 'job_dispatches', ['status', 'lease_expires_at'], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_job_dispatches_lease_expires', table_name='job_dispatches')
    op.drop_index('ix_job_dispatches_status_created', table_name='job_dispatches')
    op.drop_index('ix_job_dispatches_tenant_id', table_name='job_dispatches')
    op.drop_index('ix_job_dispatches_status', table_name='job_dispatches')
    op.drop_index('ix_job_dispatches_job_id', table_name='job_dispatches')
    op.drop_table('job_dispatches')
