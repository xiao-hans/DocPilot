"""派发循环（docs/17 §4.1）。

数据库里的派发记录是唯一事实：本循环只做三件事——回收长时间无人认领的投递、
把 ready 记录投递出去、拿到 publisher confirm 后才推进状态。发布失败时记录留在
ready 等下一轮，绝不把"发过"当成"已投递"。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import DISPATCH_BATCH_SIZE
from app.contexts.processing import dispatch
from app.contexts.processing.broker import TransportError

logger = logging.getLogger("docpilot.dispatcher")


def payload_for(row: Any) -> dict[str, Any]:
    """消息只带任务标识与最小路由信息：不含文件正文、令牌或绝对路径。"""
    return {
        "dispatch_id": row.id,
        "job_id": row.job_id,
        "tenant_id": row.tenant_id,
        "attempt_no": row.attempt_no,
        "queue": row.queue_name,
    }


class Dispatcher:
    def __init__(self, transport: Any) -> None:
        self._transport = transport

    def run_once(self) -> dict[str, int]:
        counters = {"published": 0, "failed": 0, "skipped": 0, "republished": 0}
        counters["republished"] = len(dispatch.republish_stalled())
        for row in dispatch.list_ready(DISPATCH_BATCH_SIZE):
            try:
                self._transport.publish(payload_for(row))
            except TransportError as exc:
                dispatch.note_publish_failure(row.id)
                counters["failed"] += 1
                logger.warning("派发失败 job_id=%s dispatch_id=%s：%s", row.job_id, row.id, exc)
                continue
            # 条件更新没命中说明另一个派发器已抢先推进；重复投递由 Worker 租约去重。
            if dispatch.mark_published(row.id):
                counters["published"] += 1
            else:
                counters["skipped"] += 1
        return counters
