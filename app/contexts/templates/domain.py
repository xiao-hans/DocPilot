"""模板配置上下文：文档类型与不可变字段模板版本（DOC-DD-004 §4.1/§4.2）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.platform.db import Base

TEMPLATE_STATUSES = ("draft", "active", "retired")


class Template(Base):
    """可复用的字段配置；已发布版本独立于草稿存在。"""

    __tablename__ = "templates"
    __table_args__ = (
        Index(
            "uq_template_active_name",
            "tenant_id",
            "name",
            unique=True,
            sqlite_where=text("status <> 'retired'"),
            postgresql_where=text("status <> 'retired'"),
        ),
        UniqueConstraint("tenant_id", "id", name="uq_template_tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", index=True)
    draft_fields: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    versions: Mapped[list[TemplateVersion]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )


class TemplateVersion(Base):
    """发布即冻结的字段快照；无更新与删除路径。"""

    __tablename__ = "template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "revision", name="uq_template_version_revision"),
        UniqueConstraint("tenant_id", "id", name="uq_template_version_tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    template_id: Mapped[str] = mapped_column(
        ForeignKey("templates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    fields: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    published_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    template: Mapped[Template] = relationship(back_populates="versions")
