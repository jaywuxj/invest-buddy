"""QuoteHub：富途订阅推送（主）+ gtimg 轮询（备/交叉校验）。

- 富途可用且 OpenD 在线 → 秒级推送驱动预警评估与 SSE；
- 否则 gtimg 定时轮询兜底；
- 每次报价更新：发布 SSE quote 事件 + 评估预警规则（事件驱动，替代每小时空跑）。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from . import portfolio
from .app_settings import get_settings
from .alerts import evaluate_rules, process_hits
from .events import bus
from .providers import FutuQuoteSubscriber, GtimgQuoteProvider, futu_available
from .settings import QUOTE_POLL_INTERVAL, QUOTE_POLL_MARKET_ONLY
from .store import log_run

_gtimg = GtimgQuoteProvider()
_futu: FutuQuoteSubscriber | None = None
_latest: dict[str, dict] = {}  # gtimg code -> quote
_started = False


def latest_quotes() -> dict[str, dict]:
    return dict(_latest)


def _in_market_hours() -> bool:
    """粗略交易时段：工作日全天覆盖港股 09:30-16:00 与美股 21:30-05:00（夏令时）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        # 周一清晨仍在美股周五盘中（北京时间 05:00 前）
        if not (now.weekday() == 5 and now.hour < 6):
            return False
    return True


async def _on_quotes(quotes: dict[str, dict], source: str) -> None:
    if not quotes:
        return
    _latest.update(quotes)
    bus.publish("quote", {"source": source, "quotes": quotes})
    rules = portfolio.load_alert_rules() if get_settings()["auto"].get("alert_watch", True) else []
    if rules:
        hits = evaluate_rules(rules, quotes, portfolio.gtimg_to_code())
        await process_hits(hits)


async def _poll_loop() -> None:
    while True:
        try:
            if not QUOTE_POLL_MARKET_ONLY or _in_market_hours():
                codes = portfolio.gtimg_codes()
                if codes:
                    quotes = await _gtimg.get_quotes(codes)
                    await _on_quotes(quotes, "gtimg")
        except Exception as e:
            log_run("quotehub", "error", f"gtimg 轮询失败: {e}")
        await asyncio.sleep(QUOTE_POLL_INTERVAL)


async def _futu_loop() -> None:
    """富途主通道：订阅成功后保持；异常退出则回到 gtimg（轮询 loop 始终在跑）。"""
    global _futu
    if not futu_available():
        log_run("quotehub", "info", "futu-api 未安装，实时推送降级为 gtimg 轮询")
        return
    codes = [s["code"] for s in portfolio.all_stocks() if s.get("code")]
    if not codes:
        return

    def on_quote(futu_code: str, quote: dict) -> None:
        # futu code (HK.00700) → gtimg key 对齐（hk00700），供预警评估复用
        sym = futu_code.split(".")[-1]
        market = futu_code.split(".")[0].upper()
        gkey = ("hk" if market == "HK" else "us" if market == "US" else "sh") + sym
        asyncio.ensure_future(_on_quotes({gkey: quote}, "futu"))

    try:
        _futu = FutuQuoteSubscriber(on_quote)
        await _futu.start(codes)
        log_run("quotehub", "ok", f"富途订阅已启动（{len(codes)} 只）")
    except Exception as e:
        log_run("quotehub", "error", f"富途订阅启动失败，使用 gtimg 兜底: {e}")
        _futu = None


async def start_quotehub() -> None:
    global _started
    if _started:
        return
    _started = True
    asyncio.create_task(_poll_loop())
    asyncio.create_task(_futu_loop())


def quotehub_status() -> dict:
    return {
        "futu_available": futu_available(),
        "futu_active": _futu is not None,
        "gtimg_breaker": _gtimg.breaker.state(),
        "cached_codes": len(_latest),
        "poll_interval": QUOTE_POLL_INTERVAL,
        "market_only": QUOTE_POLL_MARKET_ONLY,
    }
