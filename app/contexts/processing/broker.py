"""RabbitMQ 传输层（docs/17 §3/§4）。

只负责"把一条最小派发载荷投递到持久队列并拿到 publisher confirm"与"按手动
ack 消费"。业务状态一律不在这里判定：消息丢失、重复投递、连接中断都由派发记录
和租约条件更新兜住。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pika

from app.config import BROKER_URL, QUEUE_NAME, WORKER_PREFETCH


class TransportError(RuntimeError):
    """投递失败：调用方必须把派发记录留在 ready，不能伪装成已投递。"""


class AmqpTransport:
    def __init__(self, url: str | None = None, queue: str | None = None) -> None:
        self._url = url or BROKER_URL
        self.queue = queue or QUEUE_NAME
        self._connection: pika.BlockingConnection | None = None
        self._channel = None

    def _ensure_channel(self):
        if self._channel is not None and self._channel.is_open:
            return self._channel
        self.close()
        try:
            connection = pika.BlockingConnection(pika.URLParameters(self._url))
        except Exception as exc:  # 连接失败原因很多（认证、网络、broker 重启），统一成类型化错误
            raise TransportError(f"无法连接 broker：{exc}") from exc
        channel = connection.channel()
        channel.confirm_delivery()
        channel.queue_declare(queue=self.queue, durable=True)
        self._connection = connection
        self._channel = channel
        return channel

    def publish(self, payload: dict[str, Any]) -> None:
        """发布持久消息；未拿到 confirm（nack/不可路由）即视为失败。"""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            message_id=str(payload.get("dispatch_id")),
        )
        try:
            self._ensure_channel().basic_publish(
                exchange="", routing_key=self.queue, body=body, properties=properties
            )
        except pika.exceptions.UnroutableError as exc:
            raise TransportError(f"消息不可路由：{exc}") from exc
        except (pika.exceptions.AMQPError, OSError) as exc:
            self.close()
            raise TransportError(f"投递失败：{exc}") from exc

    def consume(self, handler: Callable[[dict[str, Any]], bool]) -> None:
        """按 prefetch 手动 ack 消费。handler 返回 True 表示该消息已可确认。"""
        channel = self._ensure_channel()
        channel.basic_qos(prefetch_count=WORKER_PREFETCH)

        def _on_message(_channel, method, _properties, body):
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # 载荷损坏的消息不可能被执行，直接丢弃，避免毒消息堵住队列。
                _channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                return
            if handler(payload):
                _channel.basic_ack(delivery_tag=method.delivery_tag)
            else:
                _channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

        channel.basic_consume(queue=self.queue, on_message_callback=_on_message)
        channel.start_consuming()

    def stop(self) -> None:
        """请求消费循环退出。

        BlockingConnection 不是线程安全的：从别的线程直接 close 会让 IO 循环撞上
        半关闭状态，因此用 pika 的 threadsafe 回调把停止动作投递回连接自己的线程。
        """
        connection, channel = self._connection, self._channel
        if connection is None or channel is None or not connection.is_open:
            return

        def _stop() -> None:
            if channel.is_open:
                channel.stop_consuming()

        connection.add_callback_threadsafe(_stop)

    def close(self) -> None:
        channel, connection = self._channel, self._connection
        self._channel = None
        self._connection = None
        # 断开是尽力而为：重连前旧连接可能已经被 broker 单方向关掉。
        for target in (channel, connection):
            try:
                if target is not None and target.is_open:
                    target.close()
            except Exception:
                pass
        self._channel = None
        self._connection = None
