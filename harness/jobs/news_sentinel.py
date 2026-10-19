"""news-sentinel：重大事件哨兵（工作日 21:00 + 可随时手动触发）。"""
from __future__ import annotations

from .. import portfolio
from ..providers import AkshareProvider, FinnhubProvider, GtimgQuoteProvider
from ..quality import numbers_from_digest
from ..skills import registry
from ..store import data_path
from .common import run_skill_pipeline

_ak = AkshareProvider()
_finn = FinnhubProvider()
_gtimg = GtimgQuoteProvider()


async def run() -> dict:
    stocks = portfolio.all_stocks()
    news_by_code: dict[str, list] = {}
    for s in stocks:
        code = s.get("code", "")
        items: list = []
        try:
            items += await _ak.get_news(code)
        except Exception:
            pass
        if code.upper().endswith(".US"):
            items += await _finn.get_news(code)
        if items:
            news_by_code[code] = items[:10]

    quotes = {}
    try:
        quotes = await _gtimg.get_quotes(portfolio.gtimg_codes())
    except Exception:
        pass

    context = {
        "stocks": stocks,
        "news_by_code": news_by_code,
        "quotes": quotes,
    }
    skill = registry.get("news-sentinel")
    out = await run_skill_pipeline(
        skill,
        context,
        data_path("events.json"),
        source_numbers=numbers_from_digest(quotes),
    )
    return {"ok": True, "codes_with_news": len(news_by_code), "events": len(out.get("events", []))}
