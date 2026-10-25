"""snapshot retirement R6: drop the processing_jobs snapshot column

Revision ID: c9d2e7f4a1b8
Revises: a8c41f2b6e07
Create Date: 2026-10-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9d2e7f4a1b8'
down_revision: Union[str, None] = 'a8c41f2b6e07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # R6（docs/20 §6）：读路径自 R5 起只认规范化源，四条门禁于 2026-10-25 全部满足后删列。
    # 数据级不可逆：列内容只保存在删列前的纯逻辑导出里（scripts/snapshot_logical_export.py）。
    with op.batch_alter_table('processing_jobs') as batch:
        batch.drop_column('snapshot')


def downgrade() -> None:
    # 结构可回退、数据不可回退：还原出的是带空默认值的空列，历史行不伪装仍有快照。
    # R5 之后的读路径本就走规范化列，空列不影响旧版本代码读任务详情。
    with op.batch_alter_table('processing_jobs') as batch:
        batch.add_column(
            sa.Column('snapshot', sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
