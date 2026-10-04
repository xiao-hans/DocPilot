"""tenant quotas (documents and storage bytes)

Revision ID: c3f0a5d18e42
Revises: b7c1d4e9a052
Create Date: 2026-10-04 13:02:30.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3f0a5d18e42'
down_revision: Union[str, None] = 'b7c1d4e9a052'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tenant_quotas',
        sa.Column('tenant_id', sa.String(length=32), nullable=False),
        sa.Column('max_documents', sa.Integer(), nullable=False),
        sa.Column('max_storage_bytes', sa.BigInteger(), nullable=False),
        sa.Column('set_by', sa.String(length=32), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['set_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('tenant_id'),
    )


def downgrade() -> None:
    op.drop_table('tenant_quotas')
