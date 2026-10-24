"""检索读模型：可搜索内容物化成行，租户与来源都进约束（DOC-DD-006 §3/§4）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

SEARCH_KINDS = ("filename", "text", "field")
DEFAULT_FIELD_NAME = ""


class SearchEntry(Base):
    """一个文件版本的一种可搜索来源；内容随源数据同事务重建。"""

    __tablename__ = "search_entries"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "kind", "field_name", name="uq_search_entry_source"
        ),
        Index("ix_search_entries_tenant_kind", "tenant_id", "kind"),
        Index("ix_search_entries_document_id", "document_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False, default=DEFAULT_FIELD_NAME)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
