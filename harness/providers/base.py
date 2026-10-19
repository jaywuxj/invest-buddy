"""Provider 抽象与熔断器：连失 N 次熔断 M 分钟，期间直接判定不可用走备源。"""
from __future__ import annotations

import time
from typing import Any, Protocol


class QuoteProvider(Protocol):
    name: str

    async def get_quotes(self, codes: list[str]) -> dict[str, dict]:
        """返回 {code: {name, price, prev_close, open, high, low, pct, time, currency}}"""
        ...


class FundamentalProvider(Protocol):
    name: str

    async def get_fundamentals(self, code: str) -> dict[str, Any]:
        """返回 {valuation, latest, annual, as_of}，尽力而为，缺失字段给 None。"""
        ...


class NewsProvider(Protocol):
    name: str

    async def get_news(self, code: str, days: int = 7) -> list[dict]:
        """返回 [{title, source, url, published_at, summary}]"""
        ...


class CircuitBreaker:
    def __init__(self, fail_threshold: int = 3, cooldown_sec: float = 600) -> None:
        self.fail_threshold = fail_threshold
        self.cooldown_sec = cooldown_sec
        self._fails = 0
        self._open_until = 0.0

    def available(self) -> bool:
        return time.time() >= self._open_until

    def record_success(self) -> None:
        self._fails = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        self._fails += 1
        if self._fails >= self.fail_threshold:
            self._open_until = time.time() + self.cooldown_sec
            self._fails = 0

    def state(self) -> dict:
        return {"open": not self.available(), "fails": self._fails}
