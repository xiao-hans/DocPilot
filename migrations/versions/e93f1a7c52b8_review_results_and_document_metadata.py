"""review results and document library metadata

Revision ID: e93f1a7c52b8
Revises: c7b52d1e9a44
Create Date: 2026-10-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e93f1a7c52b8"
down_revision: Union[str, None] = "c7b52d1e9a44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_texts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("document_version_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_version_id", name="uq_document_text_version"),
    )
    op.create_index("ix_document_texts_tenant_id", "document_texts", ["tenant_id"])

    op.create_table(
        "extraction_results",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("document_version_id", sa.String(length=32), nullable=False),
        sa.Column("template_version_id", sa.String(length=32), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("doc_type", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("confirmed_by", sa.String(length=32), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["processing_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "template_version_id"],
            ["template_versions.tenant_id", "template_versions.id"],
            name="fk_result_template_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "document_version_id", name="uq_result_job_version"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_result_tenant_id"),
    )
    op.create_index("ix_extraction_results_tenant_id", "extraction_results", ["tenant_id"])
    op.create_index("ix_extraction_results_job_id", "extraction_results", ["job_id"])
    op.create_index(
        "ix_extraction_results_document_version_id",
        "extraction_results",
        ["document_version_id"],
    )
    op.create_index("ix_extraction_results_state", "extraction_results", ["state"])
    op.create_index(
        "ix_extraction_results_template_version_id", "extraction_results", ["template_version_id"]
    )
    op.create_index(
        "ix_extraction_results_updated_at", "extraction_results", ["updated_at"]
    )

    op.create_table(
        "result_revisions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("result_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("actor_user_id", sa.String(length=32), nullable=True),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["result_id"], ["extraction_results.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("result_id", "revision_no", name="uq_result_revision_no"),
    )
    op.create_index("ix_result_revisions_result_id", "result_revisions", ["result_id"])
    op.create_index("ix_result_revisions_tenant_id", "result_revisions", ["tenant_id"])

    op.create_table(
        "document_labels",
        sa.Column("document_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("document_id", "label", name="pk_document_label"),
        sa.UniqueConstraint("tenant_id", "document_id", "label", name="uq_document_label"),
    )
    op.create_index("ix_document_labels_tenant_label", "document_labels", ["tenant_id", "label"])
    op.create_index("ix_document_labels_document_id", "document_labels", ["document_id"])

    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("doc_type", sa.String(length=120), nullable=True))
        batch.add_column(
            sa.Column("source", sa.String(length=20), nullable=False, server_default="upload")
        )
        batch.add_column(
            sa.Column(
                "retention_status", sa.String(length=20), nullable=False, server_default="active"
            )
        )
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_documents_retention_status", ["retention_status"])


def downgrade() -> None:
    op.drop_index("ix_document_labels_document_id", table_name="document_labels")
    op.drop_index("ix_document_labels_tenant_label", table_name="document_labels")
    op.drop_table("document_labels")
    with op.batch_alter_table("documents") as batch:
        batch.drop_index("ix_documents_retention_status")
        batch.drop_column("updated_at")
        batch.drop_column("retention_status")
        batch.drop_column("source")
        batch.drop_column("doc_type")

    op.drop_index("ix_result_revisions_tenant_id", table_name="result_revisions")
    op.drop_index("ix_result_revisions_result_id", table_name="result_revisions")
    op.drop_table("result_revisions")
    op.drop_index("ix_extraction_results_updated_at", table_name="extraction_results")
    op.drop_index(
        "ix_extraction_results_template_version_id", table_name="extraction_results"
    )
    op.drop_index("ix_extraction_results_state", table_name="extraction_results")
    op.drop_index(
        "ix_extraction_results_document_version_id", table_name="extraction_results"
    )
    op.drop_index("ix_extraction_results_job_id", table_name="extraction_results")
    op.drop_index("ix_extraction_results_tenant_id", table_name="extraction_results")
    op.drop_table("extraction_results")
    op.drop_index("ix_document_texts_tenant_id", table_name="document_texts")
    op.drop_table("document_texts")
