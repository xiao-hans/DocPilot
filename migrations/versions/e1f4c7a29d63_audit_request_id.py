"""audit events gain a request_id column (NFR-005)

Revision ID: e1f4c7a29d63
Revises: d5e2b74c0a19
Create Date: 2026-10-10 21:05:00.000000

可空且不给服务端默认值：历史审计行没有关联 ID，用默认值填一个等于编造关联。
索引用于"拿用户报的 ID 去审计表里查这一次请求做了什么"。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1f4c7a29d63'
down_revision: Union[str, None] = 'd5e2b74c0a19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('audit_events', sa.Column('request_id', sa.String(length=64), nullable=True))
    op.create_index('ix_audit_events_request_id', 'audit_events', ['request_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_audit_events_request_id', table_name='audit_events')
    op.drop_column('audit_events', 'request_id')