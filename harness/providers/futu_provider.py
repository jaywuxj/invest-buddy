"""富途 OpenD 订阅推送（可选依赖 + 需本地 OpenD 网关常驻）。

P0 定位：未安装 futu-api 或 OpenD 未运行时优雅降级（quotehub 自动回退 gtimg 轮询）。
推广期内境内认证客户港股 LV2 / 美股 LV3 免费，是实时性的主通道。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from ..settings import FUTU_HOST, FUTU_PORT
from .base import CircuitBreaker

try:
    from futu import OpenQuoteContext, RET_OK, SubType, StockQuoteHandlerBase  # type: ignore

    _AVAILABLE = True
except Exception:
    OpenQuoteContext = None  # type: ignore
    _AVAILABLE = False


def futu_available() -> bool:
    return _AVAILABLE


class FutuQuoteSubscriber:
    """订阅推送：收到报价回调 → on_quote(code, quote_dict)。断线由 quotehub 监控重启。"""

    name = "futu"

    def __init__(self, on_quote: Callable[[str, dict], None]) -> None:
        self.on_quote = on_quote
        self.breaker = CircuitBreaker()
        self._ctx = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _futu_code(self, code: str) -> str:
        """00700.HK -> HK.00700；AAPL.US -> US.AAPL"""
        sym, _, suffix = code.upper().partition(".")
        return f"{suffix}.{sym}" if suffix else code

    async def start(self, codes: list[str]) -> None:
        if not _AVAILABLE:
            raise RuntimeError("futu-api 未安装（pip install futu-api）")
        self._loop = asyncio.get_running_loop()

        provider = self

        class Handler(StockQuoteHandlerBase):  # type: ignore
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret != RET_OK:
                    return RET_OK, data
                try:
                    for _, row in data.iterrows():
                        code = str(row.get("code", ""))
                        last = row.get("last_price")
                        prev = row.get("prev_close_price")
                        pct = None
                        try:
                            if last and prev:
                                pct = round((float(last) - float(prev)) / float(prev) * 100, 2)
                        except Exception:
                            pass
                        quote = {
                            "name": str(row.get("name", "")),
                            "price": float(last) if last else None,
                            "prev_close": float(prev) if prev else None,
                            "open": float(row["open_price"]) if row.get("open_price") else None,
                            "high": float(row["high_price"]) if row.get("high_price") else None,
                            "low": float(row["low_price"]) if row.get("low_price") else None,
                            "pct": pct,
                            "time": str(row.get("update_time", "")),
                        }
                        # futu 回调在自身线程，回填到 asyncio 循环
                        if provider._loop:
                            provider._loop.call_soon_threadsafe(provider.on_quote, code, quote)
                except Exception:
                    pass
                return RET_OK, data

        def _subscribe():
            ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
            ctx.set_handler(Handler())
            futu_codes = [self._futu_code(c) for c in codes]
            ret, err = ctx.subscribe(futu_codes, [SubType.QUOTE], subscribe_push=True)
            if ret != RET_OK:
                ctx.close()
                raise RuntimeError(f"富途订阅失败: {err}")
            return ctx

        self._ctx = await asyncio.get_running_loop().run_in_executor(None, _subscribe)
        self.breaker.record_success()

    async def stop(self) -> None:
        if self._ctx is not None:
            ctx = self._ctx
            self._ctx = None
            await asyncio.get_running_loop().run_in_executor(None, ctx.close)
