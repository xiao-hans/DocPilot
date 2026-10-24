"""租户配额（FR-042 的"配额强制"那一半，2026-10-04 业主口径：机制交付、默认不限、值由管理员设）。

只有两个轴，且都对着 `docs/07` 用量视图已经在算的东西：文档数与存储字节。没有计费、没有
速率、没有按人配额——那些要先有被批准的业务口径，不在这里替业主发明。

**默认即不限**：没有 `tenant_quotas` 行等于没有配额，行为与本轮之前完全一致。只有管理员显式
写下 0 以上的数字，拒绝才会发生。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db import Base

UNLIMITED = 0


class TenantQuota(Base):
    __tablename__ = "tenant_quotas"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    max_documents: Mapped[int] = mapped_column(Integer, nullable=False, default=UNLIMITED)
    # 字节数走 BigInteger：SQLite 的 Integer 是 64 位，PostgreSQL 的 INTEGER 不是，
    # 一个 1TB 目标用 32 位列存会在远超上限前先溢出。
    max_storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=UNLIMITED)
    set_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
