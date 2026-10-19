"""AKShare 基本面/研报/新闻（可选依赖，未安装时自动不可用）。

注意：AKShare 为爬虫聚合，源站改版会失灵——所有调用 try/except 兜底，
同步阻塞接口统一跑在线程池，避免卡住事件循环。
"""
from __future__ import annotations

import asyncio
from typing import Any

from .base import CircuitBreaker

try:
    import akshare as ak  # type: ignore

    _AVAILABLE = True
except Exception:
    ak = None
    _AVAILABLE = False


def _market_of(code: str) -> str:
    c = code.upper()
    if c.endswith(".HK"):
        return "hk"
    if c.endswith(".US") or ("." not in c and c.isalpha()):
        return "us"
    return "cn"


def _symbol(code: str) -> str:
    return code.split(".")[0]


def _latest_records(df, n: int) -> list[dict] | None:
    """取最新 N 条记录。东财财报接口统一按报告期**降序**返回（最新在前），
    直接用 head；无日期列时退化为 head。历史教训：误用 tail 会拿到最旧数据
    （NVDA 曾取到 FY2005-2007）。"""
    if df is None:
        return None
    try:
        if "REPORT_DATE" in df.columns:
            df = df.sort_values("REPORT_DATE", ascending=False)
        return df.head(n).to_dict("records")
    except Exception:
        return df.head(n).to_dict("records")


class AkshareProvider:
    """基本面主力源：A股/港股/美股财报摘要、东财研报、个股新闻。尽力而为。"""

    name = "akshare"

    def __init__(self) -> None:
        self.breaker = CircuitBreaker()

    @property
    def available(self) -> bool:
        return _AVAILABLE and self.breaker.available()

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kwargs))

    async def get_fundamentals(self, code: str) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("akshare 不可用（未安装或熔断中）")
        market = _market_of(code)
        sym = _symbol(code)
        try:
            if market == "hk":
                # 港股关键指标（ROE/负债率/营收利润增速——价值投资框架核心字段）
                ind = await self._run(ak.stock_financial_hk_analysis_indicator_em, symbol=sym)
                data: dict[str, Any] = {"annual_indicators": _latest_records(ind, 5)}
                # 利润表近三年（补充收入/利润绝对值）
                try:
                    inc = await self._run(
                        ak.stock_financial_hk_report_em, stock=sym, symbol="利润表", indicator="年度"
                    )
                    data["annual_income_tail"] = _latest_records(inc, 3)
                except Exception:
                    data["annual_income_tail"] = None
            elif market == "us":
                df = await self._run(ak.stock_financial_us_analysis_indicator_em, symbol=sym)
                data = {"annual_indicators_tail": _latest_records(df, 3)}
            else:
                df = await self._run(ak.stock_financial_abstract, symbol=sym)
                data = {"abstract_tail": _latest_records(df, 5)}
            self.breaker.record_success()
            return {"code": code, "market": market, "as_of": _today(), **data}
        except Exception as e:
            self.breaker.record_failure()
            raise RuntimeError(f"akshare get_fundamentals({code}) 失败: {e}") from e

    async def get_research_reports(self, code: str) -> list[dict]:
        """东财个股研报（评级 + 未来三年盈利预测，部分覆盖 neodata 缺口）。

        实测仅覆盖 A 股；港股/美股返回空列表（东财研报库以 A 股代码为主键）。
        """
        if not self.available:
            return []
        if _market_of(code) != "cn":
            return []
        sym = _symbol(code)
        try:
            df = await self._run(ak.stock_research_report_em, symbol=sym)
            if df is None or df.empty:
                return []
            # 接口按代码过滤，但仍做一次防御性筛选
            if "股票代码" in df.columns:
                df = df[df["股票代码"].astype(str).str.zfill(6) == sym.zfill(6)]
            self.breaker.record_success()
            return df.head(10).to_dict("records")
        except Exception:
            self.breaker.record_failure()
            return []

    async def get_news(self, code: str, days: int = 7) -> list[dict]:
        if not self.available:
            return []
        sym = _symbol(code)
        try:
            df = await self._run(ak.stock_news_em, symbol=sym)
            if df is None:
                return []
            self.breaker.record_success()
            return df.head(15).to_dict("records")
        except Exception:
            self.breaker.record_failure()
            return []


def _today() -> str:
    from datetime import date

    return date.today().isoformat()
