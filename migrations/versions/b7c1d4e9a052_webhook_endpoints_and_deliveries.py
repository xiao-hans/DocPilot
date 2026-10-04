"""webhook endpoints and delivery rows

Revision ID: b7c1d4e9a052
Revises: f2a6c81b4d73
Create Date: 2026-10-04 03:15:30.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c1d4e9a052'
down_revision: Union[str, None] = 'f2a6c81b4d73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'webhook_endpoints',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('tenant_id', sa.String(length=32), nullable=False),
        sa.Column('url', sa.String(length=500), nullable=False),
        sa.Column('description', sa.String(length=120), nullable=False),
        sa.Column('event_types', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('secret_version', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('grace_seconds', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'url', name='uq_webhook_endpoint_url'),
    )
    op.create_index(
        'ix_webhook_endpoint_tenant_status', 'webhook_endpoints', ['tenant_id', 'status'], unique=False
    )
    op.create_index('ix_webhook_endpoints_tenant_id', 'webhook_endpoints', ['tenant_id'], unique=False)

    op.create_table(
        'webhook_deliveries',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('endpoint_id', sa.String(length=32), nullable=False),
        sa.Column('tenant_id', sa.String(length=32), nullable=False),
        sa.Column('event_id', sa.String(length=32), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('claimed_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lease_token', sa.String(length=64), nullable=True),
        sa.Column('response_status', sa.Integer(), nullable=True),
        sa.Column('error_kind', sa.String(length=16), nullable=True),
        sa.Column('last_error', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['endpoint_id'], ['webhook_endpoints.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint_id', 'event_id', name='uq_webhook_delivery_endpoint_event'),
    )
    op.create_index(
        'ix_webhook_delivery_due', 'webhook_deliveries', ['status', 'next_attempt_at'], unique=False
    )
    op.create_index(
        'ix_webhook_deliveries_endpoint_id', 'webhook_deliveries', ['endpoint_id'], unique=False
    )
    op.create_index(
        'ix_webhook_deliveries_tenant_id', 'webhook_deliveries', ['tenant_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_webhook_deliveries_tenant_id', table_name='webhook_deliveries')
    op.drop_index('ix_webhook_deliveries_endpoint_id', table_name='webhook_deliveries')
    op.drop_index('ix_webhook_delivery_due', table_name='webhook_deliveries')
    op.drop_table('webhook_deliveries')
    op.drop_index('ix_webhook_endpoints_tenant_id', table_name='webhook_endpoints')
    op.drop_index('ix_webhook_endpoint_tenant_status', table_name='webhook_endpoints')
    op.drop_table('webhook_endpoints')
