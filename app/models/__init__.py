"""平台级 ORM 模型：账号、租户、文档、任务、派发与 outbox。

这个包**只**装 `app/models/` 下的表。上下文自己的表（`processing`、`review`、
`templates`、`search`、`automation`、`quota`、`webhooks`）由各自的 `domain.py`
定义，本包不导入它们——导入会让模型层反向依赖业务上下文（`docs/12 §7.1` R5）。

全表注册发生在 `migrations/env.py`：它是 `Base.metadata` 的唯一消费者。
"""

from app.models import access, account, dispatch, document, outbox, persistent_job  # noqa: F401

__all__ = ["access", "account", "dispatch", "document", "outbox", "persistent_job"]
