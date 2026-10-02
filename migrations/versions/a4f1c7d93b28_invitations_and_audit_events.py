"""invitations and audit events

Revision ID: a4f1c7d93b28
Revises: e3a9c2d4176f
Create Date: 2026-10-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4f1c7d93b28"
down_revision: Union[str, None] = "e3a9c2d4176f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("invited_by", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["accepted_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invitations_tenant_id", "invitations", ["tenant_id"])
    op.create_index("ix_invitations_status", "invitations", ["status"])
    op.create_index("ix_invitations_invited_by", "invitations", ["invited_by"])
    op.create_index(
        "uq_invitations_token_hash",
        "invitations",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "uq_invitation_pending_email",
        "invitations",
        ["tenant_id", "email"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("object_type", sa.String(length=32), nullable=False),
        sa.Column("object_id", sa.String(length=32), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_actor_user_id", "audit_events", ["actor_user_id"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index(
        "ix_audit_events_tenant_created", "audit_events", ["tenant_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_tenant_created", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_user_id", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("uq_invitation_pending_email", table_name="invitations")
    op.drop_index("uq_invitations_token_hash", table_name="invitations")
    op.drop_index("ix_invitations_invited_by", table_name="invitations")
    op.drop_index("ix_invitations_status", table_name="invitations")
    op.drop_index("ix_invitations_tenant_id", table_name="invitations")
    op.drop_table("invitations")
