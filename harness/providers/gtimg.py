"""gtimg 行情（移植自 panel/server.js 的解析逻辑，字段口径保持一致）。

协议：https://qt.gtimg.cn/q=hk00700,usAAPL → GBK 文本 v_xxx="f0~f1~..."
关键字段：f1=名称 f3=现价 f4=昨收 f5=今开 f30=时间 f33=最高 f34=最低
"""
from __future__ import annotations

import re
import time

import httpx

from ..settings import QUOTE_CACHE_TTL_SEC
from .base import CircuitBreaker

_RE = re.compile(r'v_([a-zA-Z0-9]+)="([^"]*)"')


def _num(s: str) -> float | None:
    try:
        v = float(s)
        return v if v == v else None  # NaN 过滤
    except (ValueError, TypeError):
        return None


def parse_gtimg(text: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in _RE.finditer(text):
        key, payload = m.group(1), m.group(2)
        f = payload.split("~")
        if len(f) < 6 or not f[3]:
            continue
        price, prev = _num(f[3]), _num(f[4])
        pct = round((price - prev) / prev * 100, 2) if price is not None and prev else None
        out[key] = {
            "name": f[1] or "",
            "price": price,
            "prev_close": prev,
            "open": _num(f[5]),
            "high": _num(f[33]) if len(f) > 33 else None,
            "low": _num(f[34]) if len(f) > 34 else None,
            "pct": pct,
            "time": f[30] if len(f) > 30 else "",
            # 港/美股：f39=市盈率（静态口径） f45=总市值（亿，交易币种）。汇率报文无这两位。
            "pe": _num(f[39]) if len(f) > 45 else None,
            "market_cap": _num(f[45]) if len(f) > 45 else None,
        }
    return out


class GtimgQuoteProvider:
    name = "gtimg"

    def __init__(self) -> None:
        self.breaker = CircuitBreaker()
        self._cache: tuple[float, str, dict] = (0.0, "", {})

    async def get_quotes(self, codes: list[str]) -> dict[str, dict]:
        if not self.breaker.available():
            raise RuntimeError("gtimg 熔断中")
        key = ",".join(codes)
        now = time.time()
        c_at, c_key, c_data = self._cache
        if c_data and c_key == key and now - c_at < QUOTE_CACHE_TTL_SEC:
            return c_data
        url = "https://qt.gtimg.cn/q=" + key
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                text = resp.content.decode("gbk", errors="replace")
        except Exception:
            self.breaker.record_failure()
            raise
        data = parse_gtimg(text)
        if not data:
            self.breaker.record_failure()
            raise RuntimeError("gtimg 返回空数据")
        self.breaker.record_success()
        self._cache = (now, key, data)
        return data
