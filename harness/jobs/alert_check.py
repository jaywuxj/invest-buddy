"""alert-check：预警安全网（定时兜底）。主力是 quotehub 事件驱动，本任务每小时补一次。"""
from __future__ import annotations

from .. import portfolio
from ..alerts import evaluate_rules, process_hits
from ..providers import GtimgQuoteProvider
from ..store import log_run


async def run() -> dict:
    codes = portfolio.gtimg_codes()
    if not codes:
        log_run("alert-check", "skip", "无持仓配置")
        return {"ok": False, "reason": "no_holdings"}
    provider = GtimgQuoteProvider()
    quotes = await provider.get_quotes(codes)
    rules = portfolio.load_alert_rules()
    hits = evaluate_rules(rules, quotes, portfolio.gtimg_to_code())
    fresh = await process_hits(hits)
    log_run("alert-check", "ok", f"规则 {len(rules)} 条，命中 {len(hits)}，新推送 {len(fresh)}")
    return {"ok": True, "rules": len(rules), "hits": len(hits), "fresh": len(fresh)}
