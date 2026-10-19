"""进程内事件总线：quote 更新 / 预警命中 / job 状态 → SSE 订阅者。"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event_type: str, payload: Any) -> None:
        msg = {"type": event_type, "ts": time.time(), "data": payload}
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # 慢消费者丢弃最旧消息，永不阻塞生产者
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except Exception:
                    pass


bus = EventBus()
