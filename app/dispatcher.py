"""派发器进程（docs/17 §3/§4）：`python -m app.dispatcher`

每轮做三件事，全部是幂等条件更新，重复执行安全：

1. 超时回收：把卡在 processing 的任务收敛或退回 queued（唯一恢复入口）。
2. 投递回收：把长时间无人认领的派发行退回 ready，防止消息静默丢失。
3. 发布：把 ready 记录投递到队列，拿到 publisher confirm 才推进状态。
"""

from __future__ import annotations

import logging
import threading

from app.config import DISPATCH_POLL_SECONDS, QUEUE_ENABLED
from app.contexts.processing import service as processing
from app.contexts.processing.broker import AmqpTransport
from app.contexts.processing.dispatcher import Dispatcher
from app.db import SessionLocal
from app.request_context import configure_logging

logger = logging.getLogger("docpilot.dispatcher.main")


def run_round(transport: AmqpTransport, runner: Dispatcher) -> dict[str, int]:
    with SessionLocal() as db:
        reclaimed = processing.reclaim_timeouts(db)
    counters = runner.run_once()
    counters["reclaimed"] = sum(len(ids) for ids in reclaimed.values())
    if any(counters.values()):
        logger.info("派发轮次 %s", counters)
    return counters


def main() -> None:
    if not QUEUE_ENABLED:
        raise SystemExit("未配置 BROKER_URL：队列模式未启用，任务仍由 API 进程内执行")
    configure_logging()
    transport = AmqpTransport()
    runner = Dispatcher(transport)
    while True:
        try:
            run_round(transport, runner)
        except KeyboardInterrupt:
            transport.close()
            return
        except Exception as exc:  # broker 或数据库抖动都不能让派发停摆
            logger.warning("派发轮次失败，%s 秒后重试：%s", DISPATCH_POLL_SECONDS, exc)
        threading.Event().wait(DISPATCH_POLL_SECONDS)


if __name__ == "__main__":
    main()
