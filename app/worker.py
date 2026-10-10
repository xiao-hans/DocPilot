"""独立 Worker 进程（docs/17 §4.2）：`python -m app.worker`

消息只用于"叫醒"。能不能执行、执行结果能不能写库，全部由数据库里的租约条件
更新判定：重复投递拿不到第二次租约，被接管的旧 Worker 会在收口时被条件更新拒绝。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.config import DISPATCH_POLL_SECONDS, JOB_LEASE_SECONDS, QUEUE_ENABLED
from app.contexts.processing import dispatch
from app.contexts.processing.broker import AmqpTransport
from app.request_context import configure_logging
from app.services.job_manager import get_job_manager

logger = logging.getLogger("docpilot.worker")


def _renew_loop(lease: dispatch.Lease, stop: threading.Event) -> None:
    """周期性续租；续租失败意味着租约已被接管，本次结果会在收口时被拒绝写入。"""
    interval = max(5, JOB_LEASE_SECONDS // 3)
    while not stop.wait(interval):
        if not dispatch.renew_lease(lease.dispatch_id, lease.token):
            logger.warning("续租失败，租约可能已被接管 dispatch_id=%s", lease.dispatch_id)
            return


def handle(payload: Any) -> bool:
    """处理一条派发消息。返回 True 表示该消息可以确认掉。"""
    if not isinstance(payload, dict):
        logger.warning("忽略非对象载荷")
        return True
    dispatch_id = str(payload.get("dispatch_id") or "")
    job_id = str(payload.get("job_id") or "")
    tenant_id = str(payload.get("tenant_id") or "")
    attempt_no = payload.get("attempt_no")
    if not dispatch_id or not job_id or not tenant_id or not isinstance(attempt_no, int):
        # 结构不完整的消息不可能被执行，直接确认以免堵住队列。
        logger.warning("忽略结构不完整的派发消息：%s", sorted(payload))
        return True

    token = dispatch.claim_lease(
        dispatch_id, tenant_id=tenant_id, job_id=job_id, attempt_no=attempt_no
    )
    if token is None:
        # 已有执行者持有有效租约，或该派发已有结局：重复投递的正常分支。
        # 记一条 INFO：这是"消息被确认掉但任务没执行"的唯一现场证据，缺了它就只能靠猜。
        logger.info("未领到租约，跳过 dispatch_id=%s job_id=%s", dispatch_id, job_id)
        return True

    lease = dispatch.Lease(dispatch_id=dispatch_id, token=token)
    stop = threading.Event()
    renewer = threading.Thread(target=_renew_loop, args=(lease, stop), daemon=True)
    renewer.start()
    try:
        get_job_manager().process(job_id, lease=lease)
        logger.info("执行完成 job_id=%s dispatch_id=%s", job_id, dispatch_id)
    except Exception as exc:
        # 失败尝试已由处理链路在同一事务里记录并关闭派发；重投同一条消息不会改变结果。
        logger.exception("执行失败 job_id=%s：%s", job_id, exc)
    finally:
        stop.set()
    return True


def main() -> None:
    if not QUEUE_ENABLED:
        raise SystemExit("未配置 BROKER_URL：任务仍由 API 进程内执行，无需启动 Worker")
    configure_logging()
    transport = AmqpTransport()
    while True:
        try:
            transport.consume(handle)
        except KeyboardInterrupt:
            transport.close()
            return
        except Exception as exc:  # broker 重启或网络中断：退避后重新消费，队列里的消息不丢
            logger.warning("消费中断，%s 秒后重连：%s", DISPATCH_POLL_SECONDS, exc)
            transport.close()
            threading.Event().wait(DISPATCH_POLL_SECONDS)


if __name__ == "__main__":
    main()
