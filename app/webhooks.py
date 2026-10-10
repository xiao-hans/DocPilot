"""Webhook 投递进程：`python -m app.webhooks`（DOC-DD-021 §7）

与派发器同构但不依赖 `BROKER_URL`：出站通知和队列是两条独立链路，没启用队列的部署
照样应该有 Webhook。主密钥缺失时直接拒绝启动，而不是"起来但一条都发不出去"。
"""

from __future__ import annotations

import logging
import threading

from app.config import WEBHOOK_MASTER_KEY, WEBHOOK_POLL_SECONDS
from app.contexts.webhooks.delivery import HttpxTransport, deliver_round
from app.contexts.webhooks.service import require_master_key
from app.request_context import configure_logging


def main() -> None:
    require_master_key()  # 没配主密钥就抛，不让进程带着"永远发不出去"的状态长跑
    configure_logging()
    transport = HttpxTransport()
    logger = logging.getLogger("docpilot.webhooks.main")
    logger.info("Webhook 投递已启动（轮询 %s 秒）", WEBHOOK_POLL_SECONDS)
    while True:
        try:
            deliver_round(transport)
        except KeyboardInterrupt:
            transport.close()
            return
        except Exception as exc:  # 数据库或网络抖动都不能让出站投递停摆
            logger.warning("Webhook 轮次失败，%s 秒后重试：%s", WEBHOOK_POLL_SECONDS, exc)
        threading.Event().wait(WEBHOOK_POLL_SECONDS)


if __name__ == "__main__":
    main()
