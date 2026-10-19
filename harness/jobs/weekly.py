"""weekly-signals（周五 16:10）+ weekly-onepager（周五 16:15，deep 级质量）。"""
from __future__ import annotations

from .. import portfolio
from ..providers import AkshareProvider, FinnhubProvider, GtimgQuoteProvider
from ..quality import numbers_from_digest
from ..skills import registry
from ..store import data_path, read_json
from ..settings import PANEL_OUT_DIR
from .common import run_skill_pipeline

_gtimg = GtimgQuoteProvider()
_ak = AkshareProvider()
_finn = FinnhubProvider()


async def _weekly_context() -> dict:
    stocks = portfolio.all_stocks()
    codes = [s["gtimg"] for s in stocks if s.get("gtimg")]
    quotes = await _gtimg.get_quotes(codes) if codes else {}

    fundamentals: dict = {}
    research: dict = {}
    for s in stocks:
        code = s.get("code", "")
        try:
            fundamentals[code] = await _ak.get_fundamentals(code)
        except Exception as e:
            fundamentals[code] = {"error": str(e)}
        try:
            research[code] = await _ak.get_research_reports(code)
        except Exception:
            research[code] = []

    # 上下文延续：仅使用 harness 自产的上一版 analysis（不引用任何 WorkBuddy 产物）
    prior_analysis = read_json(PANEL_OUT_DIR / "analysis.json", {})

    return {
        "stocks": stocks,
        "quotes": quotes,
        "fundamentals": fundamentals,
        "research": research,
        "prior_signals": prior_analysis.get("signals", [])[:20],
    }


async def run_signals() -> dict:
    ctx = await _weekly_context()
    skill = registry.get("weekly-signals")
    out = await run_skill_pipeline(
        skill,
        ctx,
        data_path("signals.json"),
        source_numbers=numbers_from_digest(ctx["quotes"]),
    )
    return {"ok": True, "signals": len(out.get("signals", []))}


async def run_onepager() -> dict:
    ctx = await _weekly_context()
    skill = registry.get("weekly-onepager")
    out = await run_skill_pipeline(
        skill,
        ctx,
        data_path("onepager.json"),
        source_numbers=numbers_from_digest(ctx["quotes"]),
    )
    return {"ok": True, "pages": len(out.get("pages", []))}
