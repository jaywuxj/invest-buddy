"""deep-diligence：个股深度研究。API 随时触发（POST /api/analyze/<code>），
取数 → skill 分析 → 质量护栏 → 写 data/diligence/<code>.json（pending_review）。
"""
from __future__ import annotations

from .. import portfolio
from ..providers import AkshareProvider, FinnhubProvider, GtimgQuoteProvider
from ..quality import numbers_from_digest
from ..skills import registry
from ..store import data_path, log_run
from .common import JobFailed, run_skill_pipeline

_gtimg = GtimgQuoteProvider()
_ak = AkshareProvider()
_finn = FinnhubProvider()


async def build_digest(code: str) -> dict:
    meta = portfolio.stock_meta(code)
    if not meta:
        raise JobFailed(f"未知标的: {code}（不在 holdings.json）")
    digest: dict = {"meta": meta}

    # 实时行情
    if meta.get("gtimg"):
        try:
            q = await _gtimg.get_quotes([meta["gtimg"]])
            digest["quote"] = q.get(meta["gtimg"])
        except Exception as e:
            digest["quote"] = None
            digest["quote_error"] = str(e)

    # 基本面（AKShare，尽力而为）
    try:
        digest["fundamentals"] = await _ak.get_fundamentals(code)
    except Exception as e:
        digest["fundamentals"] = None
        digest["fundamentals_error"] = str(e)

    # 研报评级
    try:
        digest["research_reports"] = await _ak.get_research_reports(code)
    except Exception:
        digest["research_reports"] = []

    # 新闻：AKShare 全市场 + Finnhub 补美股
    news: list = []
    try:
        news += await _ak.get_news(code)
    except Exception:
        pass
    if code.upper().endswith(".US"):
        news += await _finn.get_news(code)
    digest["news"] = news[:20]
    return digest


async def run(code: str) -> dict:
    digest = await build_digest(code)
    skill = registry.get("deep-diligence")
    out = await run_skill_pipeline(
        skill,
        {"code": code, "digest": digest},
        data_path(f"diligence/{code}.json"),
        source_numbers=numbers_from_digest(digest.get("quote")) + numbers_from_digest(digest.get("meta")),
        extra_meta={"code": code},
    )
    return {"ok": True, "code": code, "status": out["_meta"]["status"], "elapsed_ms": out["_meta"]["elapsed_ms"]}
