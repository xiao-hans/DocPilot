"""template catalog and processing reliability

Revision ID: c7b52d1e9a44
Revises: a4f1c7d93b28
Create Date: 2026-10-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7b52d1e9a44"
down_revision: Union[str, None] = "a4f1c7d93b28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "templates",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("draft_fields", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_template_tenant_id"),
    )
    op.create_index("ix_templates_tenant_id", "templates", ["tenant_id"])
    op.create_index("ix_templates_status", "templates", ["status"])
    op.create_index("ix_templates_created_by", "templates", ["created_by"])
    op.create_index(
        "uq_template_active_name",
        "templates",
        ["tenant_id", "name"],
        unique=True,
        sqlite_where=sa.text("status <> 'retired'"),
        postgresql_where=sa.text("status <> 'retired'"),
    )

    op.create_table(
        "template_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("template_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("published_by", sa.String(length=32), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["template_id"], ["templates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id", "revision", name="uq_template_version_revision"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_template_version_tenant_id"),
    )
    op.create_index("ix_template_versions_template_id", "template_versions", ["template_id"])
    op.create_index("ix_template_versions_tenant_id", "template_versions", ["tenant_id"])

    op.create_table(
        "job_attempts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engine", sa.String(length=120), nullable=False),
        sa.Column("parse_modes", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["processing_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_no", name="uq_job_attempt_number"),
    )
    op.create_index("ix_job_attempts_job_id", "job_attempts", ["job_id"])
    op.create_index("ix_job_attempts_tenant_id", "job_attempts", ["tenant_id"])
    op.create_index("ix_job_attempts_status", "job_attempts", ["status"])
    op.create_index("ix_job_attempts_job_started", "job_attempts", ["job_id", "started_at"])

    op.create_table(
        "domain_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_domain_events_tenant_id", "domain_events", ["tenant_id"])
    op.create_index("ix_domain_events_event_type", "domain_events", ["event_type"])
    op.create_index("ix_domain_events_published_at", "domain_events", ["published_at"])
    op.create_index(
        "ix_domain_events_type_occurred", "domain_events", ["event_type", "occurred_at"]
    )

    with op.batch_alter_table("processing_jobs") as batch:
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("idempotency_key", sa.String(length=200), nullable=True))
        batch.add_column(sa.Column("template_version_id", sa.String(length=32), nullable=True))
        batch.create_foreign_key(
            "fk_processing_job_template_version",
            "template_versions",
            ["tenant_id", "template_version_id"],
            ["tenant_id", "id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "uq_job_idempotency_key",
            ["tenant_id", "idempotency_key"],
            unique=True,
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        )
        batch.create_index("ix_processing_jobs_idempotency_key", ["idempotency_key"])


def downgrade() -> None:
    with op.batch_alter_table("processing_jobs") as batch:
        batch.drop_index("ix_processing_jobs_idempotency_key")
        batch.drop_index("uq_job_idempotency_key")
        batch.drop_constraint("fk_processing_job_template_version", type_="foreignkey")
        batch.drop_column("template_version_id")
        batch.drop_column("idempotency_key")
        batch.drop_column("attempt_count")
        batch.drop_column("finished_at")

    op.drop_index("ix_domain_events_type_occurred", table_name="domain_events")
    op.drop_index("ix_domain_events_published_at", table_name="domain_events")
    op.drop_index("ix_domain_events_event_type", table_name="domain_events")
    op.drop_index("ix_domain_events_tenant_id", table_name="domain_events")
    op.drop_table("domain_events")
    op.drop_index("ix_job_attempts_job_started", table_name="job_attempts")
    op.drop_index("ix_job_attempts_status", table_name="job_attempts")
    op.drop_index("ix_job_attempts_tenant_id", table_name="job_attempts")
    op.drop_index("ix_job_attempts_job_id", table_name="job_attempts")
    op.drop_table("job_attempts")
    op.drop_index("ix_template_versions_tenant_id", table_name="template_versions")
    op.drop_index("ix_template_versions_template_id", table_name="template_versions")
    op.drop_table("template_versions")
    op.drop_index("uq_template_active_name", table_name="templates")
    op.drop_index("ix_templates_created_by", table_name="templates")
    op.drop_index("ix_templates_status", table_name="templates")
    op.drop_index("ix_templates_tenant_id", table_name="templates")
    op.drop_table("templates")
