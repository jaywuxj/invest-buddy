"""Finnhub 美股新闻/基本面补充（免费档 60 次/分，需 FINNHUB_API_KEY）。"""
from __future__ import annotations

from datetime import date, timedelta

import httpx

from ..settings import FINNHUB_API_KEY
from .base import CircuitBreaker

_BASE = "https://finnhub.io/api/v1"


class FinnhubProvider:
    name = "finnhub"

    def __init__(self) -> None:
        self.breaker = CircuitBreaker()

    @property
    def available(self) -> bool:
        return bool(FINNHUB_API_KEY) and self.breaker.available()

    async def get_news(self, code: str, days: int = 7) -> list[dict]:
        if not self.available:
            return []
        sym = code.split(".")[0]
        params = {
            "symbol": sym,
            "from": (date.today() - timedelta(days=days)).isoformat(),
            "to": date.today().isoformat(),
            "token": FINNHUB_API_KEY,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(_BASE + "/company-news", params=params)
                resp.raise_for_status()
                rows = resp.json()
            self.breaker.record_success()
        except Exception:
            self.breaker.record_failure()
            return []
        return [
            {
                "title": r.get("headline", ""),
                "source": r.get("source", ""),
                "url": r.get("url", ""),
                "published_at": r.get("datetime"),
                "summary": r.get("summary", ""),
            }
            for r in (rows or [])[:15]
        ]
