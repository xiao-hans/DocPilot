"""文档与文件版本的持久化模型：文档是客户资产，任务只是它的一次处理。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.platform.db import Base


class Document(Base):
    """租户内的一份文档；一次上传创建一份，后续版本挂在其下。"""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_document_tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # FR-013 文档元数据：类型来自模板名，来源区分上传/API/导入，保留状态服务于删除流程。
    doc_type: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="upload")
    retention_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentVersion(Base):
    """不可变的文件版本记录：数据库只存相对存储键，不存对外展示的绝对路径。"""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "revision", name="uq_document_version_revision"),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_document_version_tenant_document",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 产生该版本的任务；任务记录被清理时文档版本仍保留，因此是 SET NULL 而非 CASCADE。
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # 快照退役 R1（docs/20 §2）：这四列本来就是"这一版解析出来的结果"，
    # 过去只活在 processing_jobs.snapshot 的 files[] 里，按文件查一次解析统计就得展开 JSON。
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ocr_applied: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    parse_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # 恶意文件检测（FR-010，DOC-DD-024）。NULL 表示"从未扫过"，它与 `clean` 必须能区分：
    # 没扫过的版本不能被读成"扫过且干净"。处置口径是仅标记不阻断（`docs/19 §7`），
    # 所以这两列只留痕，不参与任何状态流转。
    malware_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    malware_signature: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    document: Mapped[Document] = relationship(back_populates="versions")


class DocumentLabel(Base):
    """标签规范化存储：可索引、可分页过滤，且在 SQLite/PostgreSQL 行为一致。"""

    __tablename__ = "document_labels"
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", "label", name="uq_document_label"),
        Index("ix_document_labels_tenant_label", "tenant_id", "label"),
        Index("ix_document_labels_document_id", "document_id"),
    )

    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(30), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
