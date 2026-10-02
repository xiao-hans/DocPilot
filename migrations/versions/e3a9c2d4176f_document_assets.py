"""tenant-scoped document assets

Revision ID: e3a9c2d4176f
Revises: d8774313b162
Create Date: 2026-10-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3a9c2d4176f"
down_revision: Union[str, None] = "d8774313b162"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_document_tenant_id"),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_owner_id", "documents", ["owner_id"])

    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("document_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_document_version_tenant_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["processing_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "revision", name="uq_document_version_revision"),
        sa.UniqueConstraint("storage_key", name="uq_document_versions_storage_key"),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_index("ix_document_versions_tenant_id", "document_versions", ["tenant_id"])
    op.create_index("ix_document_versions_job_id", "document_versions", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_document_versions_job_id", table_name="document_versions")
    op.drop_index("ix_document_versions_tenant_id", table_name="document_versions")
    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_index("ix_documents_owner_id", table_name="documents")
    op.drop_index("ix_documents_tenant_id", table_name="documents")
    op.drop_table("documents")
