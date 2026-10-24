"""提取与复核上下文：原文文本、机器结果与只追加修订链（DOC-DD-005 §3/§4）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

RESULT_STATES = ("machine", "revised", "confirmed", "rejected")
REVISION_SOURCES = ("machine", "human")


class DocumentText(Base):
    """解析文本是派生数据，与文件版本一对一；预览只返回纯文本。"""

    __tablename__ = "document_texts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExtractionResult(Base):
    """一个文件版本在一个模板版本下的当前结果。"""

    __tablename__ = "extraction_results"
    __table_args__ = (
        UniqueConstraint("job_id", "document_version_id", name="uq_result_job_version"),
        UniqueConstraint("tenant_id", "id", name="uq_result_tenant_id"),
        # 版本不可删除：被引用的模板版本用 RESTRICT 保护；租户未提供删除能力，
        # 因此 tenant_id 不加 CASCADE，避免"级联删租户"与"RESTRICT 保护结果"互相矛盾。
        ForeignKeyConstraint(
            ["tenant_id", "template_version_id"],
            ["template_versions.tenant_id", "template_versions.id"],
            name="fk_result_template_version",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_version_id: Mapped[str | None] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    doc_type: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="machine", index=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confirmed_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class ResultRevision(Base):
    """只追加的修订记录：机器首条，之后每次人工保存追加一条。"""

    __tablename__ = "result_revisions"
    __table_args__ = (UniqueConstraint("result_id", "revision_no", name="uq_result_revision_no"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    result_id: Mapped[str] = mapped_column(
        ForeignKey("extraction_results.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
