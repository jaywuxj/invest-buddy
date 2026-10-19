"""preopen-brief：美股盘前简报（工作日 21:30，北京时间夏令时盘前）。"""
from __future__ import annotations

from .. import portfolio
from ..providers import FinnhubProvider, GtimgQuoteProvider
from ..quality import numbers_from_digest
from ..skills import registry
from ..store import data_path
from .common import run_skill_pipeline

_gtimg = GtimgQuoteProvider()
_finn = FinnhubProvider()


async def run() -> dict:
    stocks = portfolio.all_stocks()
    us = [s for s in stocks if str(s.get("code", "")).upper().endswith(".US")]
    codes = [s["gtimg"] for s in stocks if s.get("gtimg")]
    quotes = await _gtimg.get_quotes(codes) if codes else {}

    news: list = []
    for s in us:
        news += await _finn.get_news(s["code"], days=2)

    context = {"stocks": stocks, "us_stocks": us, "quotes": quotes, "us_news": news[:20]}
    skill = registry.get("preopen-brief")
    out = await run_skill_pipeline(
        skill,
        context,
        data_path("preopen.json"),
        source_numbers=numbers_from_digest(quotes),
    )
    return {"ok": True, "highlights": len(out.get("highlights", []))}
