"""snapshot retirement R1: normalized-source columns

Revision ID: d5e2b74c0a19
Revises: c3f0a5d18e42
Create Date: 2026-10-04 13:27:10.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e2b74c0a19'
down_revision: Union[str, None] = 'c3f0a5d18e42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 全部可空：可空即"还没回填"（docs/20 §3 R1），不用默认值去猜历史行的意思。
    with op.batch_alter_table('processing_jobs') as batch:
        batch.add_column(sa.Column('field_definitions', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('progress', sa.String(length=24), nullable=True))
    with op.batch_alter_table('document_versions') as batch:
        batch.add_column(sa.Column('page_count', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('text_chars', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('ocr_applied', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('parse_error', sa.String(length=300), nullable=True))
    with op.batch_alter_table('job_attempts') as batch:
        batch.add_column(sa.Column('warnings', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('job_attempts') as batch:
        batch.drop_column('warnings')
    with op.batch_alter_table('document_versions') as batch:
        batch.drop_column('parse_error')
        batch.drop_column('ocr_applied')
        batch.drop_column('text_chars')
        batch.drop_column('page_count')
    with op.batch_alter_table('processing_jobs') as batch:
        batch.drop_column('progress')
        batch.drop_column('field_definitions')
