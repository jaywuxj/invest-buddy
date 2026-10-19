"""面板契约数据 · 程序化生产者（零 LLM，纯取数 + 计算，数字 100% 可溯源）。

产出（均写 data/panel/，契约与原 panel 前端一致）：
- fundamentals.json  估值（PE-TTM/PB/动态 PE/总市值）+ 最新期 + 最近财年关键指标
- earnings.json      近 5 个单季（港股累计口径差分得单季）+ 规则化解读
- research.json      港股：经济通券商评级/目标价逐条；美股：Finnhub 评级分布（需 key）
- 另返回 upcoming（财报日历，估算/Finnhub 精确）与 news（harness/news.py 多源新闻）供 LLM 阶段使用

数据源：AKShare（东财财务指标、经济通盈利预测）+ 东财资讯搜索 + gtimg（行情/市值/汇率）
+ Finnhub（可选，美股评级与财报日历）。任何单股失败只影响该股，不影响整体。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
import time
from typing import Any

import httpx

from . import filings, portfolio
from .holdings_service import PortfolioError, _strict_json, _write as safe_write_json
from .providers.gtimg import GtimgQuoteProvider
from .settings import PANEL_OUT_DIR, env_key
from . import settings
from .store import atomic_write_json, read_json

try:
    import akshare as ak  # type: ignore
except Exception:  # pragma: no cover
    ak = None

_SEM = asyncio.Semaphore(4)  # AKShare 为爬虫聚合，控制并发避免被源站限流

CCY = {"人民币": "CNY", "港元": "HKD", "港币": "HKD", "美元": "USD", "新台币": "TWD"}
BUY = ("买入", "增持", "优于大市", "跑赢行业", "跑赢大市", "强烈推荐", "推荐", "买进", "优于大盘", "outperform", "buy")
SELL = ("卖出", "减持", "逊于大市", "跑输行业", "跑输大市", "沽出", "underperform", "sell")


# ---------------------------------------------------------------- 工具

def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


def _r(v: Any, n: int = 2) -> float | None:
    x = _f(v)
    return None if x is None else round(x, n)


def _yi(v: Any) -> float | None:
    x = _f(v)
    return None if x is None else round(x / 1e8, 2)


def _date(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v)[:10]
    return s if len(s) == 10 and s[4] == "-" else None


def _yoy(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 2)


async def _ak(fn, **kw):
    async with _SEM:
        return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(**kw))


def _records(df) -> list[dict]:
    if df is None or getattr(df, "empty", True):
        return []
    if "REPORT_DATE" in df.columns:
        df = df.sort_values("REPORT_DATE", ascending=False)
    return df.to_dict("records")


def _market(code: str) -> str:
    return "hk" if code.upper().endswith(".HK") else "us"


# SEC companyfacts 只含标准 taxonomy 的披露科目；这是一份重点科目摘录，不是完整三表。
_SEC_FIELDS = {
    "income": {
        "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "Revenue"),
        "operating_income": ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities"),
        "net_income": ("NetIncomeLossAvailableToCommonStockholdersBasic", "NetIncomeLoss", "ProfitLossAttributableToOwnersOfParent"),
    },
    "balance": {
        "assets": ("Assets",), "liabilities": ("Liabilities",),
        "equity": ("StockholdersEquity", "EquityAttributableToOwnersOfParent", "Equity"),
        "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalents"),
        "current_assets": ("AssetsCurrent", "CurrentAssets"),
        "current_liabilities": ("LiabilitiesCurrent", "CurrentLiabilities"),
    },
    "cashflow": {
        "operating_cashflow": ("NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"),
        "investing_cashflow": ("NetCashProvidedByUsedInInvestingActivities", "CashFlowsFromUsedInInvestingActivities"),
        "capex_payments": ("PaymentsToAcquirePropertyPlantAndEquipment", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
    },
}


def sec_statement_extract(facts_doc: dict, cik: int) -> dict:
    """按同一原始申报 accession 与年度截止日提取，不混合不同财年/币种。"""
    facts = facts_doc.get("facts") or {}
    if not isinstance(facts, dict):
        raise ValueError("SEC companyfacts 格式异常")
    annual_forms = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
    anchors = []
    for taxonomy in ("us-gaap", "ifrs-full"):
        for tag in ("Assets", "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenue"):
            concept = (facts.get(taxonomy) or {}).get(tag) or {}
            for unit, rows in (concept.get("units") or {}).items():
                if unit not in ("USD", "TWD", "HKD", "CNY") or not isinstance(rows, list):
                    continue
                anchors.extend({**r, "currency": unit} for r in rows if isinstance(r, dict)
                               and r.get("form") in annual_forms and r.get("fp") == "FY"
                               and isinstance(r.get("end"), str) and isinstance(r.get("accn"), str))
    if not anchors:
        return {"status": "unavailable", "missing": ["SEC 未提供可识别的标准 XBRL 年度科目；不从媒体/模型补值"]}
    anchor = max(anchors, key=lambda r: (r["end"], r.get("filed") or "", r["currency"] == "USD"))
    accession, end, currency = anchor["accn"], anchor["end"], anchor["currency"]
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        raise ValueError("SEC accession 格式异常")
    result: dict[str, Any] = {"status": "partial", "period_end": end, "filed": anchor.get("filed"),
                              "form": anchor.get("form"), "currency": currency, "accession": accession,
                              "filing_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/",
                              "source": "SEC EDGAR companyfacts（标准 XBRL，非完整三表）", "missing": []}
    for section, fields in _SEC_FIELDS.items():
        result[section] = {}
        for key, tags in fields.items():
            selected = None
            for tag in tags:
                for taxonomy in ("us-gaap", "ifrs-full"):
                    concept = (facts.get(taxonomy) or {}).get(tag) or {}
                    records = (concept.get("units") or {}).get(currency) or []
                    for row in records:
                        if (not isinstance(row, dict) or row.get("accn") != accession or row.get("end") != end
                                or row.get("form") not in annual_forms or not isinstance(row.get("val"), (int, float))
                                or isinstance(row.get("val"), bool) or not math.isfinite(row["val"])):
                            continue
                        if section != "balance":
                            try:
                                days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(row["start"])).days
                            except (KeyError, ValueError, TypeError):
                                continue
                            if not 300 <= days <= 390:
                                continue
                        elif row.get("start"):
                            continue
                        selected = {"value": row["val"], "unit": currency, "tag": tag, "taxonomy": taxonomy}
                        break
                    if selected:
                        break
                if selected:
                    break
            if selected:
                result[section][key] = selected
            else:
                result["missing"].append(f"{section}.{key}：该申报未提供对应标准科目")
    if not any(result[s] for s in _SEC_FIELDS):
        result["status"] = "unavailable"
    return result


_SEC_REFRESH_LOCK = asyncio.Lock()


async def refresh_sec_statements(stocks: list[dict]) -> dict:
    """每日自动增量更新美股标准科目；单股失败保留 last-good，港股不伪装成 SEC 数据。"""
    async with _SEC_REFRESH_LOCK:
        return await _refresh_sec_statements(stocks)


async def _refresh_sec_statements(stocks: list[dict]) -> dict:
    path = PANEL_OUT_DIR / "statements.json"
    if path.is_symlink() or PANEL_OUT_DIR.is_symlink():
        raise ValueError("财务科目缓存路径不安全；未覆盖原件")
    try:
        previous = _strict_json(path, ()) if path.exists() else {"items": {}}
    except PortfolioError as exc:
        raise ValueError("财务科目缓存损坏；未覆盖原件") from exc
    if not isinstance(previous.get("items"), dict):
        raise ValueError("财务科目缓存结构异常；未覆盖原件")
    items, errors, updated = dict(previous["items"]), {}, 0
    now = dt.datetime.now(dt.timezone.utc)
    async with httpx.AsyncClient(timeout=25) as client:
        for stock in stocks:
            code = stock.get("code", "")
            if not isinstance(code, str) or not code.endswith(".US"):
                continue
            old = items.get(code)
            if isinstance(old, dict) and old.get("fetched_at"):
                try:
                    if now - dt.datetime.fromisoformat(old["fetched_at"]) < dt.timedelta(hours=22):
                        continue
                except (ValueError, TypeError):
                    pass
            try:
                ticker = code.removesuffix(".US")
                cik = await filings._cik(client, ticker)
                if not cik:
                    raise ValueError("SEC 未匹配到该 ticker 的 CIK")
                response = await client.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                                            headers={"User-Agent": filings.SEC_UA})
                response.raise_for_status()
                document = response.json()
                # 美股 ADR 常指向境外发行人；不得把另一上市主体的 SEC 财报塞进同名标的。
                tickers = document.get("tickers") if isinstance(document, dict) else None
                if not isinstance(tickers, list) or ticker.upper() not in {str(t).upper() for t in tickers}:
                    raise ValueError("SEC companyfacts 标的代码与本地代码不一致")
                obj = sec_statement_extract(document, cik)
                obj.update({"code": code, "name": stock.get("name"), "fetched_at": now.isoformat()})
                if obj["status"] == "unavailable" and isinstance(old, dict) and old.get("status") != "unavailable":
                    errors[code] = "最新数据无标准科目，保留已有快照"
                    continue
                items[code] = obj
                updated += 1
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError, TimeoutError) as exc:
                errors[code] = f"SEC 接口或数据不可用：{type(exc).__name__}"
    if updated:
        safe_write_json(path, {"generated_at": now.isoformat(), "source": "SEC EDGAR companyfacts 标准 XBRL",
                               "items": items})
    return {"updated": updated, "errors": errors, "source": "SEC EDGAR companyfacts"}


# ---------------------------------------------------------------- 行情 / 汇率

async def fetch_quotes_and_fx() -> tuple[dict, dict]:
    """返回 (code -> quote, fx)；fx 为 1 单位外币兑人民币。"""
    gt = GtimgQuoteProvider()
    stocks = portfolio.all_stocks()
    codes = [s["gtimg"] for s in stocks if s.get("gtimg")] + ["whHKDCNY", "whUSDCNY"]
    raw = await gt.get_quotes(codes)
    by_code = {s["code"]: raw.get(s["gtimg"]) for s in stocks if s.get("gtimg") and raw.get(s["gtimg"])}
    fx = {"CNY": 1.0}
    if raw.get("whHKDCNY"):
        fx["HKD"] = raw["whHKDCNY"]["price"]
    if raw.get("whUSDCNY"):
        fx["USD"] = raw["whUSDCNY"]["price"]
    return by_code, fx


def _convert(price: float | None, from_ccy: str, to_ccy: str, fx: dict) -> float | None:
    if price is None:
        return None
    if from_ccy == to_ccy:
        return price
    if from_ccy in fx and to_ccy in fx:
        return price * fx[from_ccy] / fx[to_ccy]
    return None  # 无汇率（如 TWD）→ 不计算，宁缺毋错


# ---------------------------------------------------------------- 港股：累计口径 → 单季

def _hk_label(month: int, fye: int) -> str:
    k = (month - fye) % 12
    return {3: "Q1", 6: "H1", 9: "Q1-Q3", 0: "FY"}.get(k, f"M{month}")


def _fy_of(d: dt.date, fye: int) -> int:
    return d.year if d.month <= fye else d.year + 1


def _hk_single_periods(recs: list[dict], fye: int) -> list[dict]:
    """东财港股「报告期」为年内累计值：单季 = 本期累计 − 上期累计（同财年内）。"""
    rows = []
    for r in recs:
        d = _date(r.get("REPORT_DATE"))
        if not d:
            continue
        dd = dt.date.fromisoformat(d)
        rows.append((dd, _fy_of(dd, fye), (dd.month - fye) % 12 or 12, r))
    rows.sort(key=lambda x: x[0])
    by_fy: dict[int, list] = {}
    for row in rows:
        by_fy.setdefault(row[1], []).append(row)

    singles: dict[tuple[int, str], dict] = {}
    for fy, lst in by_fy.items():
        prev = None
        for dd, _, k, r in lst:
            rev, np_, gp, eps = (_f(r.get(x)) for x in ("OPERATE_INCOME", "HOLDER_PROFIT", "GROSS_PROFIT", "BASIC_EPS"))
            ocf_r = _f(r.get("OCF_SALES"))  # 经营现金流 / 营收（%，年内累计口径）→ 还原累计经营现金流
            ocf = ocf_r / 100 * rev if ocf_r is not None and rev is not None else None
            if prev is None:
                label = {3: "Q1", 6: "H1"}.get(k)
                if not label:
                    prev = (k, rev, np_, gp, eps, ocf)
                    continue
                s_rev, s_np, s_gp, s_eps, s_ocf = rev, np_, gp, eps, ocf
                span = k
                direct_yoy = (_r(r.get("OPERATE_INCOME_YOY")), _r(r.get("HOLDER_PROFIT_YOY")))
            else:
                pk = prev[0]
                label = {(3, 6): "Q2", (6, 9): "Q3", (9, 12): "Q4", (6, 12): "H2", (3, 12): "Q2-Q4"}.get((pk, k))
                if not label:
                    prev = (k, rev, np_, gp, eps, ocf)
                    continue
                sub = lambda a, b: None if a is None or b is None else a - b  # noqa: E731
                s_rev, s_np, s_gp, s_eps, s_ocf = sub(rev, prev[1]), sub(np_, prev[2]), sub(gp, prev[3]), sub(eps, prev[4]), sub(ocf, prev[5])
                span = k - pk
                direct_yoy = (None, None)
            singles[(fy, label)] = {
                "period": f"{fy}-{label}",
                "publish": None,  # 东财港股指标无公告日字段
                "revenue": _yi(s_rev),
                "net_profit": _yi(s_np),
                "gross_margin": _r(s_gp / s_rev * 100) if s_gp is not None and s_rev else None,
                "net_margin": _r(s_np / s_rev * 100) if s_np is not None and s_rev else None,
                "eps": _r(s_eps, 3),
                "ocf": _yi(s_ocf),
                "end": dd.isoformat(), "months": span,
                **_cal_label(dd, span, f"{fy}-{label}", fye != 12),
                "_raw": (s_rev, s_np),
                "_direct_yoy": direct_yoy,
                "_date": dd,
            }
            prev = (k, rev, np_, gp, eps, ocf)

    out = []
    for (fy, label), p in singles.items():
        last = singles.get((fy - 1, label))
        ry, ny = p["_direct_yoy"]
        p["rev_yoy"] = ry if ry is not None else (_yoy(p["_raw"][0], last["_raw"][0]) if last else None)
        p["np_yoy"] = ny if ny is not None else (_yoy(p["_raw"][1], last["_raw"][1]) if last else None)
        out.append(p)
    out.sort(key=lambda x: x["_date"], reverse=True)
    for p in out:
        for k in ("_raw", "_direct_yoy", "_date"):
            p.pop(k, None)
    # 只保留与最新期同跨度的期别（季报公司全是单季、半年报公司全是半年），趋势图口径一致；最多 8 期
    if out:
        out = [p for p in out if p["months"] == out[0]["months"]]
    return out[:8]


def _cal_label(end: dt.date, months: int, fiscal: str, fiscal_differs: bool) -> dict:
    """自然季度/半年标签：期末日+跨度 → 「2026 年 4-6 月」「2026Q2」；财年口径与自然年不一致时保留财年标签。"""
    m0 = (end.month - months) % 12 + 1
    y0 = end.year if m0 <= end.month else end.year - 1
    span = f"{m0}-{end.month} 月" if y0 == end.year else f"{y0} 年 {m0} 月-{end.year} 年 {end.month} 月"
    if months == 3 and end.month % 3 == 0:
        short = f"{end.year}Q{end.month // 3}"
    elif months == 6 and end.month in (6, 12):
        short = f"{end.year}H{1 if end.month == 6 else 2}"
    else:  # 非自然季度末（如英伟达 5-7 月）：不硬套 Q 编号，直接写月份
        short = f"{str(y0)[2:]}年{m0}-{end.month}月" if y0 == end.year else f"{str(y0)[2:]}年{m0}月-{end.month}月"
    natural = short if months in (3, 6) else None
    differs = fiscal_differs or (natural is not None and natural.replace("Q", "-Q").replace("H", "-H") != fiscal)
    return {"label": short, "label_cn": (f"{end.year} 年 " if y0 == end.year else "") + span,
            "fiscal": ("FY" + fiscal.replace("-", " ")[2:] if differs else None)}


def _metric_block(r: dict, period: str, *, roe_ann: float | None, roic: float | None) -> dict:
    rev, np_ = _yi(r.get("OPERATE_INCOME")), _yi(r.get("HOLDER_PROFIT") or r.get("PARENT_HOLDER_NETPROFIT"))
    ry = _r(r.get("OPERATE_INCOME_YOY"))
    ny = _r(r.get("HOLDER_PROFIT_YOY") if r.get("HOLDER_PROFIT_YOY") is not None else r.get("PARENT_HOLDER_NETPROFIT_YOY"))
    note = []
    if rev is not None:
        note.append(f"营收 {rev:,.2f} 亿" + (f"（{ry:+.2f}%）" if ry is not None else ""))
    if np_ is not None:
        note.append(f"归母净利 {np_:,.2f} 亿" + (f"（{ny:+.2f}%）" if ny is not None else ""))
    return {
        "period": period,
        "eps": _r(r.get("BASIC_EPS"), 3),
        "roe": _r(r.get("ROE_AVG")),
        "roe_annualized": roe_ann,
        "gross_margin": _r(r.get("GROSS_PROFIT_RATIO")),
        "net_margin": _r(r.get("NET_PROFIT_RATIO")),
        "debt_ratio": _r(r.get("DEBT_ASSET_RATIO")),
        "rev_yoy": ry,
        "roic": roic,
        "ocf_sales": _r(r.get("OCF_SALES")),
        "note": "，".join(note) or None,
    }


# ---------------------------------------------------------------- 单股采集

async def _collect_hk(code: str) -> dict:
    sym = code.split(".")[0]
    rep = _records(await _ak(ak.stock_financial_hk_analysis_indicator_em, symbol=sym, indicator="报告期"))
    ann = _records(await _ak(ak.stock_financial_hk_analysis_indicator_em, symbol=sym, indicator="年度"))
    if not rep and not ann:
        raise RuntimeError("东财港股财务指标为空")
    fye = dt.date.fromisoformat(_date(ann[0]["REPORT_DATE"])).month if ann else 12
    out: dict[str, Any] = {"ccy": CCY.get(str((rep or ann)[0].get("CURRENCY")), "CNY"), "fye": fye}
    if rep:
        r0 = rep[0]
        d0 = dt.date.fromisoformat(_date(r0["REPORT_DATE"]))
        lab = _hk_label(d0.month, fye)
        out["latest"] = _metric_block(r0, f"{_fy_of(d0, fye)}{lab}", roe_ann=_r(r0.get("ROE_YEARLY")), roic=_r(r0.get("ROIC_YEARLY")))
        out["eps_ttm"], out["bps"] = _f(r0.get("EPS_TTM")), _f(r0.get("BPS"))
        out["periods"] = _hk_single_periods(rep, fye)
        out["latest_end"] = d0
        out["quarterly"] = any(dt.date.fromisoformat(_date(x["REPORT_DATE"])).month in ((fye + 3) % 12 or 12, (fye + 9) % 12 or 12) for x in rep[:6] if _date(x.get("REPORT_DATE")))
    if ann:
        a0 = ann[0]
        d = dt.date.fromisoformat(_date(a0["REPORT_DATE"]))
        out["annual"] = _metric_block(a0, f"{_fy_of(d, fye)}FY", roe_ann=_r(a0.get("ROE_YEARLY") or a0.get("ROE_AVG")), roic=_r(a0.get("ROIC_YEARLY")))
        out["annual_np"] = _yi(a0.get("HOLDER_PROFIT"))
        out["annual_fy"] = _fy_of(d, fye)
    # 经济通：券商评级/目标价逐条 + 综合一致预期（部分标的无覆盖，失败不影响财务数据）
    try:
        out["brokers"] = (await _ak(ak.stock_hk_profit_forecast_et, symbol=sym, indicator="盈利预测概览")).to_dict("records")
    except Exception:
        out["brokers"] = []
    try:
        out["consensus"] = (await _ak(ak.stock_hk_profit_forecast_et, symbol=sym, indicator="综合盈利预测")).to_dict("records")
    except Exception:
        out["consensus"] = []
    return out


async def _collect_us(code: str) -> dict:
    sym = code.split(".")[0]
    q = _records(await _ak(ak.stock_financial_us_analysis_indicator_em, symbol=sym, indicator="单季报"))
    a = _records(await _ak(ak.stock_financial_us_analysis_indicator_em, symbol=sym, indicator="年报"))
    if not q and not a:
        raise RuntimeError("东财美股财务指标为空")
    out: dict[str, Any] = {"ccy": CCY.get(str((q or a)[0].get("CURRENCY")), "USD")}
    if q:
        q0 = q[0]
        roe_q = _r(q0.get("ROE_AVG"))
        out["latest"] = _metric_block(q0, f"{_us_period(q0)}（单季）",
                                      roe_ann=_r(roe_q * 4) if roe_q is not None else None, roic=None)
        out["latest"]["note"] = (out["latest"]["note"] or "") + "；ROE 年化=单季×4"
        out["periods"] = [{
            "period": _us_period(r),
            "publish": _date(r.get("NOTICE_DATE")),
            "end": _date(r.get("REPORT_DATE")), "months": 3,
            **(_cal_label(dt.date.fromisoformat(_date(r.get("REPORT_DATE"))), 3, _us_fiscal(r), False) if _date(r.get("REPORT_DATE")) else {}),
            "revenue": _yi(r.get("OPERATE_INCOME")),
            "rev_yoy": _r(r.get("OPERATE_INCOME_YOY")),
            "net_profit": _yi(r.get("PARENT_HOLDER_NETPROFIT")),
            "np_yoy": _r(r.get("PARENT_HOLDER_NETPROFIT_YOY")),
            "gross_margin": _r(r.get("GROSS_PROFIT_RATIO")),
            "net_margin": _r(r.get("NET_PROFIT_RATIO")),
            "eps": _r(r.get("BASIC_EPS"), 3),
        } for r in q[:8]]
        out["last_notice"] = _date(q0.get("NOTICE_DATE"))
    if a:
        out["annual"] = _metric_block(a[0], _us_period(a[0]).replace("-", ""), roe_ann=_r(a[0].get("ROE_AVG")), roic=None)
    return out


def _us_fiscal(r: dict) -> str:
    """东财美股 REPORT_TYPE 的年份是财年起始年（微软 2026-06 季标 2025/Q4）；按公司惯例改为财年结束年（FY26 Q4）。"""
    m = re.match(r"(\d{4})-Q([1-4])$", _us_period(r))
    end = _date(r.get("REPORT_DATE"))
    if not m or not end:
        return _us_period(r)
    e = dt.date.fromisoformat(end)
    fy_end_month = e.month + 3 * (4 - int(m.group(2)))
    fy = e.year + (fy_end_month - 1) // 12
    return f"{fy}-Q{m.group(2)}"


def _us_period(r: dict) -> str:
    """东财美股 REPORT_TYPE 形如 2026/Q2、2025/FY（按公司财年口径）；DATE_TYPE 只是「单季报/年报」字样。"""
    rt = str(r.get("REPORT_TYPE") or "").strip()
    return rt.replace("/", "-") if rt else (_date(r.get("REPORT_DATE")) or "--")


async def _finnhub(path: str, **params) -> Any:
    key = env_key("FINNHUB_API_KEY")
    if not key:
        return None
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"https://finnhub.io/api/v1/{path}", params={**params, "token": key})
        if r.status_code != 200:
            return None
        return r.json()


# ---------------------------------------------------------------- 组装三份契约

def _rating_bucket(s: str) -> str:
    s = str(s or "").strip().lower()
    if any(k.lower() in s for k in SELL):
        return "sell"
    if any(k.lower() in s for k in BUY):
        return "buy"
    return "hold"


def _build_research_hk(code: str, name: str, raw: dict, quote: dict | None, today: str) -> dict | None:
    brokers = raw.get("brokers") or []
    if not brokers:
        return None
    # 每家券商只取最新一条（同一券商会对多个财年给预测，目标价相同）
    latest: dict[str, dict] = {}
    for b in brokers:
        org = str(b.get("证券商") or "").strip()
        if not org:
            continue
        cur = latest.get(org)
        if cur is None or str(b.get("更新日期")) > str(cur.get("更新日期")):
            latest[org] = b
    rows = sorted(latest.values(), key=lambda x: str(x.get("更新日期")), reverse=True)
    tps = [_f(x.get("目标价")) for x in rows if _f(x.get("目标价"))]
    buckets = [_rating_bucket(x.get("评级")) for x in rows]
    n = len(buckets) or 1
    item: dict[str, Any] = {
        "name": name,
        "currency": "HKD",
        "target_count": len(tps),
        "target_avg": _r(sum(tps) / len(tps)) if tps else None,
        "target_max": _r(max(tps)) if tps else None,
        "target_min": _r(min(tps)) if tps else None,
        "current_price": quote.get("price") if quote else None,
        "rating_latest": {
            "month": str(rows[0].get("更新日期"))[:7] if rows else today[:7],
            "buy": round(buckets.count("buy") / n * 100),
            "hold": round(buckets.count("hold") / n * 100),
            "sell": round(buckets.count("sell") / n * 100),
            "count": len(buckets),
        },
        "reports": [{
            "date": str(x.get("更新日期"))[:10],
            "org": x.get("证券商"),
            "title": f"{x.get('证券商')}：{x.get('评级')}" + (f"，目标价 {_r(x.get('目标价'))} 港元" if _f(x.get("目标价")) else ""),
            "rating": x.get("评级"),
            "summary": (f"{x.get('财政年度')} 财年预测纯利 {_f(x.get('纯利/亏损')) / 100:,.2f} 亿" if _f(x.get("纯利/亏损")) else "")
                       + (f"，EPS {_f(x.get('每股盈利')) / 100:.2f}" if _f(x.get("每股盈利")) else ""),
            "report_id": None,
        } for x in rows[:6]],
    }
    cons = raw.get("consensus") or []
    if cons:
        c0 = sorted(cons, key=lambda x: str(x.get("财政年度")))[0]
        np_est = _f(c0.get("纯利/亏损"))
        np_est_yi = np_est / 100 if np_est is not None else None
        yoy = _yoy(np_est_yi, raw.get("annual_np")) if str(c0.get("财政年度")) == str((raw.get("annual_fy") or 0) + 1) else None
        item["next_report"] = {
            "period": f"{c0.get('财政年度')}FY（全年一致预期）",
            "publish_date": None,
            "np_estimate": f"{np_est_yi:,.2f} 亿" if np_est_yi is not None else None,
            "np_yoy": yoy,
            "rev_estimate": None,
            "rev_yoy": None,
        }
        eps_fwd = _f(c0.get("每股盈利/每股亏损"))
        item["_eps_forward"] = eps_fwd / 100 if eps_fwd else None
    return item


async def _finnhub_us(code: str, today: dt.date) -> dict:
    """美股 Finnhub 免费档三件套（逐股查询：TSM 会被映射为 2330.TW，全局日历按 symbol 匹配会错配）。
    - stock/recommendation：评级分布（月度）
    - calendar/earnings：下期精确财报日 + EPS/营收一致预期
    - stock/metric：PE-TTM / 动态 PE / PB / PS / 股息率
    stock/price-target 为付费接口（403），目标价缺口保留。
    """
    sym = code.split(".")[0]
    rec, cal, met = await asyncio.gather(
        _finnhub("stock/recommendation", symbol=sym),
        _finnhub("calendar/earnings", symbol=sym, **{"from": today.isoformat(), "to": (today + dt.timedelta(days=150)).isoformat()}),
        _finnhub("stock/metric", symbol=sym, metric="all"),
        return_exceptions=True,
    )
    ok = lambda x: None if isinstance(x, Exception) else x  # noqa: E731
    cal_list = sorted(((ok(cal) or {}).get("earningsCalendar") or []), key=lambda e: e.get("date") or "9999")
    return {"rec": ok(rec) or [], "next": cal_list[0] if cal_list else None, "metric": (ok(met) or {}).get("metric") or {}}


def _build_research_us(code: str, name: str, quote: dict | None, fh: dict, periods: list[dict]) -> dict | None:
    rec, nxt = fh.get("rec") or [], fh.get("next")
    if not rec and not nxt:
        return None
    item: dict[str, Any] = {
        "name": name,
        "currency": "USD",
        "target_count": 0,
        "target_avg": None, "target_max": None, "target_min": None,  # Finnhub 目标价为付费接口
        "current_price": quote.get("price") if quote else None,
        "reports": [],
    }
    if rec:
        r0 = rec[0]
        buy = (r0.get("strongBuy") or 0) + (r0.get("buy") or 0)
        hold = r0.get("hold") or 0
        sell = (r0.get("sell") or 0) + (r0.get("strongSell") or 0)
        n = buy + hold + sell or 1
        item["rating_latest"] = {"month": str(r0.get("period"))[:7], "buy": round(buy / n * 100),
                                 "hold": round(hold / n * 100), "sell": round(sell / n * 100), "count": buy + hold + sell}
    if nxt:
        rev_est = _f(nxt.get("revenueEstimate"))
        eps_est = _f(nxt.get("epsEstimate"))
        # 同比基准：单季序列（新→旧）第 4 条应为去年同季——校验季度号一致才使用，否则宁缺毋错
        base = periods[3] if len(periods) >= 4 and str(periods[3].get("period", "")).endswith(f"Q{nxt.get('quarter')}") else None
        base_rev = base.get("revenue") if base else None
        base_eps = base.get("eps") if base else None
        # TSM 在 Finnhub 映射为 2330.TW：预期值为新台币、台股每股口径，与东财 TSM 口径一致（均为 2330.TW）
        ccy_note = "（新台币，2330.TW 口径）" if str(nxt.get("symbol", "")).endswith(".TW") else ""
        item["next_report"] = {
            "period": f"FY{nxt.get('year')} Q{nxt.get('quarter')}（公司财年）{ccy_note}",
            "publish_date": f"{nxt.get('date')} {'盘前' if nxt.get('hour') == 'bmo' else '盘后' if nxt.get('hour') == 'amc' else ''}".strip(),
            "np_estimate": f"EPS {eps_est:.2f}" if eps_est is not None else None,
            "np_yoy": _yoy(eps_est, base_eps),  # 口径说明：此处为 EPS 一致预期同比
            "rev_estimate": f"{rev_est / 1e8:,.2f} 亿" if rev_est else None,
            "rev_yoy": _yoy(rev_est / 1e8, base_rev) if rev_est and base_rev else None,
        }
    return item


def _us_ocf_fill(code: str, periods: list[dict]) -> None:
    """美股单季经营现金流：westock cashflow CFO_Q / income Sales_Q 的比例 × 本表营收（单位口径自洽，TSM 等也成立）。
    结果缓存 24h（data/panel/ocf_cache.json）；westock 缺失或失败如实留空。"""
    from .providers import westock
    path = settings.PANEL_OUT_DIR / "ocf_cache.json"
    cache = read_json(path, {}) or {}
    hit = cache.get(code)
    if not (hit and time.time() - hit.get("ts", 0) < 86400):
        if not westock.available():
            return
        ws = "us" + code.split(".")[0]
        try:
            cf = {r.get("EndDate"): westock.num(r.get("CFO_Q")) for r in westock.finance(ws, "cashflow", 8)}
            inc = {r.get("EndDate"): westock.num(r.get("Sales_Q")) for r in westock.finance(ws, "income", 8)}
        except Exception:
            return
        ratio = {str(d)[:7]: cf[d] / inc[d] for d in cf if cf.get(d) is not None and inc.get(d)}  # 按年月对齐（财季末日两源可能差几天）
        hit = {"ts": time.time(), "ratio": ratio}
        cache[code] = hit
        atomic_write_json(path, cache)
    for p in periods:
        rt = (hit.get("ratio") or {}).get(str(p.get("end") or "")[:7])
        if rt is not None and p.get("revenue") is not None:
            p["ocf"] = _r(rt * p["revenue"])


def _expect_track(code: str, nxt: dict | None, periods: list[dict], today: dt.date) -> dict | None:
    """披露前快照一致预期（Finnhub 下季营收/EPS），披露后与实际比对出「超/不及预期」。
    快照在披露日前持续用最新预期覆盖，披露后冻结；按披露日（±3 天）匹配实际期，避免财年编号口径差异。"""
    path = settings.PANEL_OUT_DIR / "earnings_expect.json"
    book = read_json(path, {}) or {}
    mine = book.setdefault(code, {})
    if nxt and nxt.get("date") and today.isoformat() < str(nxt["date"]):
        rev = _f(nxt.get("revenueEstimate"))
        mine[str(nxt["date"])] = {"taken": today.isoformat(), "fiscal": f"FY{nxt.get('year')} Q{nxt.get('quarter')}",
                                  "rev_est": _r(rev / 1e8) if rev else None, "eps_est": _r(_f(nxt.get("epsEstimate")), 3)}
        book[code] = dict(sorted(mine.items())[-6:])
        atomic_write_json(path, book)
    if not periods or not periods[0].get("publish"):
        return None
    p0, pub = periods[0], dt.date.fromisoformat(periods[0]["publish"])
    for d, snap in mine.items():
        if abs((dt.date.fromisoformat(d) - pub).days) <= 3 and snap.get("taken", "") < periods[0]["publish"]:
            out = {"publish": d, "taken": snap["taken"]}
            if snap.get("rev_est") and p0.get("revenue") is not None:
                out.update(rev_est=snap["rev_est"], rev_pct=_r((p0["revenue"] / snap["rev_est"] - 1) * 100))
            if snap.get("eps_est") and p0.get("eps") is not None:
                out.update(eps_est=snap["eps_est"], eps_pct=_r((p0["eps"] / snap["eps_est"] - 1) * 100) if snap["eps_est"] > 0 else None)
            return out if len(out) > 2 else None
    return None


def _earnings_analysis(periods: list[dict]) -> list[dict]:
    """规则化解读（不经 LLM，零幻觉）：增长 / 盈利 / 毛利率趋势。"""
    if not periods:
        return []
    p0 = periods[0]
    out = []
    ry, ny = p0.get("rev_yoy"), p0.get("np_yoy")
    if ry is not None:
        tag = "good" if ry >= 10 else "warn" if ry < 0 else "neutral"
        out.append({"tag": tag, "text": f"增长：{p0['period']} 营收 {p0.get('revenue') or '--'} 亿（同比 {ry:+.2f}%）"})
    if ny is not None:
        tag = "good" if ny >= 10 else "warn" if ny < 0 else "neutral"
        out.append({"tag": tag, "text": f"盈利：归母净利 {p0.get('net_profit') or '--'} 亿（同比 {ny:+.2f}%），净利率 {p0.get('net_margin') or '--'}%"})
    gms = [p.get("gross_margin") for p in periods[:4] if p.get("gross_margin") is not None]
    if len(gms) >= 2:
        diff = round(gms[0] - gms[-1], 2)
        tag = "good" if diff > 0.5 else "warn" if diff < -0.5 else "neutral"
        out.append({"tag": tag, "text": f"毛利率：最新 {gms[0]}%，较 {len(gms)} 期前 {'+' if diff >= 0 else ''}{diff} 个百分点"})
    return out


def _upcoming(code: str, name: str, raw: dict, today: dt.date) -> dict | None:
    """财报日历估算（程序规则，明确标注「估算」）。美股有 Finnhub 时会被精确日期覆盖。"""
    if _market(code) == "us":
        ln = raw.get("last_notice")
        if not ln:
            return None
        est = dt.date.fromisoformat(ln) + dt.timedelta(days=91)
        note = f"估算：上次公告 {ln} + 91 天，以公司公告为准"
        period = None
    else:
        end = raw.get("latest_end")
        if not end:
            return None
        step = 3 if raw.get("quarterly") else 6
        nxt_end = _month_end_after(end, step)
        k = (nxt_end.month - raw.get("fye", 12)) % 12
        lag = {0: 80, 6: 60}.get(k, 50)
        est = nxt_end + dt.timedelta(days=lag)
        period = f"{_fy_of(nxt_end, raw.get('fye', 12))}{_hk_label(nxt_end.month, raw.get('fye', 12))}"
        note = f"估算：期末 {nxt_end} + {lag} 天（港交所披露节奏），以公司公告为准"
    if est < today:
        return None
    if (est - today).days > 150:
        return None
    return {"code": code, "name": name, "date": est.isoformat(), "period": period, "note": note}


def _month_end_after(d: dt.date, months: int) -> dt.date:
    """d 之后第 months 个月的月末。"""
    idx = d.year * 12 + (d.month - 1) + months + 1  # 目标月的下一个月
    first_of_next = dt.date(idx // 12, idx % 12 + 1, 1)
    return first_of_next - dt.timedelta(days=1)


async def _news(stock: dict, days: int = 2) -> list[dict]:  # 用户定稿：动态只保留最近 3 天（cutoff=今天-2 天）
    """多源新闻（harness/news.py）：东财资讯按公司名搜 50 条 + Finnhub 英文 → 相关性过滤/同类合并/去重。
    旧实现 akshare.stock_news_em 按代码搜且只返回 10 条，港股几乎全是每日回购快讯。"""
    from . import news

    try:
        return await news.fetch_news(stock, days=days)
    except Exception:
        return []


async def _assemble_one(s: dict, raw: dict, q: dict | None, fh: dict, today: dt.date, fx: dict) -> tuple:
    """单股组装 fundamentals/earnings/research/upcoming 四项（全量与单股刷新共用）。"""
    code = s["code"]
    name = s["name"]
    fundamentals: dict = {}
    earnings: dict = {}
    research: dict = {}
    upcoming: list = []
    trade_ccy = s.get("currency") or ("HKD" if _market(code) == "hk" else "USD")
    # --- research（先算，pe_forward 依赖一致预期 EPS）
    if _market(code) == "hk":
        rs = _build_research_hk(code, name, raw, q, today.isoformat())
    else:
        rs = _build_research_us(code, name, q, fh, raw.get("periods") or [])
    eps_fwd = rs.pop("_eps_forward", None) if rs else None
    if rs:
        research[code] = rs
    # --- fundamentals
    price_rc = _convert(q.get("price") if q else None, trade_ccy, raw["ccy"], fx)
    val: dict[str, Any] = {"date": today.isoformat()}
    if _market(code) == "hk":
        if price_rc and raw.get("eps_ttm") and raw["eps_ttm"] > 0:
            val["pe_ttm"] = _r(price_rc / raw["eps_ttm"], 2)
        if price_rc and raw.get("bps") and raw["bps"] > 0:
            val["pb"] = _r(price_rc / raw["bps"], 2)
        if price_rc and eps_fwd and eps_fwd > 0:
            val["pe_forward"] = _r(price_rc / eps_fwd, 2)
        val["note"] = f"PE/PB 由现价×汇率({raw['ccy']}) ÷ 东财 EPS_TTM/BPS 计算；动态 PE 用经济通当年一致预期 EPS"
    else:
        m = fh.get("metric") or {}
        pe_ttm = _f(m.get("peTTM")) or (q.get("pe") if q else None)
        if pe_ttm and pe_ttm > 0:
            val["pe_ttm"] = _r(pe_ttm, 2)
        for k_out, k_in in (("pe_forward", "forwardPE"), ("pb", "pb"), ("ps_ttm", "psTTM")):
            v = _f(m.get(k_in))
            if v and v > 0:
                val[k_out] = _r(v, 2)
        dy = _f(m.get("currentDividendYieldTTM"))
        if dy is not None and dy > 0:
            val["dividend_yield"] = _r(dy, 2)
        val["note"] = "估值指标取自 Finnhub（PE-TTM/动态 PE/PB/PS/股息率）" if m else "PE(TTM) 取自腾讯行情"
    fundamentals[code] = {
        "name": name,
        "market_cap": _r(q.get("market_cap"), 2) if q else None,
        "cap_currency": trade_ccy,
        "cap_unit": "亿",
        "valuation": val,
        "latest": raw.get("latest"),
        "annual": raw.get("annual"),
    }
    # --- earnings
    if raw.get("periods"):
        earnings[code] = {
            "name": name,
            "currency": raw["ccy"],
            "unit": "亿",
            **({"note": f"报表货币为{raw['ccy']}"} if raw["ccy"] not in ("CNY",) else {}),
            "periods": raw["periods"],
            "announcements": [],
        }
        if _market(code) == "us":
            await asyncio.to_thread(_us_ocf_fill, code, raw["periods"])
        sp = _expect_track(code, fh.get("next"), raw["periods"], today)
        if sp:
            earnings[code]["surprise"] = sp
    nxt = fh.get("next")
    if nxt and nxt.get("date"):
        # Finnhub 精确日期（逐股查询，已处理 TSM→2330.TW 映射）
        upcoming.append({"code": code, "name": name, "date": nxt["date"],
                         "period": f"FY{nxt.get('year')} Q{nxt.get('quarter')}",
                         "note": "Finnhub 财报日历" + ("（盘前）" if nxt.get("hour") == "bmo" else "（盘后）" if nxt.get("hour") == "amc" else "")})
    else:
        up = _upcoming(code, name, raw, today)
        if up:
            upcoming.append(up)

    return (fundamentals.get(code), earnings.get(code), research.get(code), upcoming[-1] if upcoming else None)


async def build_program_data() -> dict:
    """全组合采集 + 组装 fundamentals / earnings / research；返回供 LLM 阶段使用的上下文。"""
    if ak is None:
        raise RuntimeError("akshare 未安装")
    t0 = time.time()
    now = time.strftime("%Y-%m-%d %H:%M")
    today = dt.date.today()
    stocks = portfolio.all_stocks()
    quotes, fx = await fetch_quotes_and_fx()

    async def one(s: dict):
        code = s["code"]
        try:
            raw = await (_collect_hk(code) if _market(code) == "hk" else _collect_us(code))
            return code, raw, None
        except Exception as e:
            return code, None, str(e)[:200]

    results = await asyncio.gather(*(one(s) for s in stocks))
    news_lists = await asyncio.gather(*(_news(s) for s in stocks))
    has_fh = bool(env_key("FINNHUB_API_KEY"))
    us_codes = [s["code"] for s in stocks if _market(s["code"]) == "us"]
    fh_all = dict(zip(us_codes, await asyncio.gather(*(_finnhub_us(c, today) for c in us_codes)))) if has_fh else {}

    fundamentals, earnings, research, upcoming, errors = {}, {}, {}, [], {}
    for s, (code, raw, err) in zip(stocks, results):
        if err or not raw:
            errors[code] = err
            continue
        fh = fh_all.get(code) or {}
        f, e, r, u = await _assemble_one(s, raw, quotes.get(code), fh, today, fx)
        if f:
            fundamentals[code] = f
        if e:
            earnings[code] = e
        if r:
            research[code] = r
        if u:
            upcoming.append(u)
    upcoming.sort(key=lambda x: x["date"])

    fx_note = "；".join(f"{k}/CNY={v:.4f}" for k, v in fx.items() if k != "CNY")
    return {
        "generated_at": now,
        "fundamentals": {"generated_at": now, "sources": [
            "东方财富财务分析指标（via AKShare，港股报告期/年度、美股单季/年报）",
            f"腾讯行情（现价/总市值）；汇率 {fx_note}",
            "港股动态 PE：经济通一致预期" + ("；美股估值：Finnhub" if has_fh else "；美股 PE：腾讯行情")], "items": fundamentals},
        "earnings": {"generated_at": now, "sources": [
            "东方财富财务分析指标（via AKShare）；港股单季由年内累计值差分计算",
            "解读为程序规则生成（增长/盈利/毛利率趋势），不经大模型"], "items": earnings},
        "research": {"generated_at": now, "source":
            "经济通券商评级/目标价（港股，via AKShare）" + ("；Finnhub 评级分布与下期一致预期（美股，目标价为付费接口暂缺）" if has_fh else "；美股机构评级待配置 FINNHUB_API_KEY"),
            "items": research},
        "upcoming": upcoming,
        "news": [n for lst in news_lists for n in lst],
        "quotes": quotes,
        "errors": errors,
        "elapsed_s": round(time.time() - t0, 1),
    }


# ---------------------------------------------------------------- 单股刷新（2026-10-16：新增股票后立即补齐该股全部程序数据）

async def refresh_one_stock(code: str) -> dict:
    """采集+组装一只股票的 fundamentals/earnings/research/news/upcoming，合并写回既有面板数据。
    其余股票的数据保持不动（只替换该股条目）。"""
    st = next((s for s in portfolio.all_stocks() if s.get("code") == code), None)
    if not st:
        return {"ok": False, "error": "unknown code"}
    today = dt.date.today()
    # 行情与汇率只拉该股：gtimg 单股 quote + 现有汇率快照（fundamentals 生成时刻的汇率即可）
    quotes_all, fx = await fetch_quotes_and_fx()  # 轻量（gtimg 批量）
    q = quotes_all.get(code)
    raw = await (_collect_hk(code) if _market(code) == "hk" else _collect_us(code))
    fh = (await _finnhub_us(code, today)) if (_market(code) == "us" and env_key("FINNHUB_API_KEY")) else {}
    news = await _news(st)
    f, e, r, u = await _assemble_one(st, raw, q, fh, today, fx)
    out: dict[str, Any] = {"ok": True, "updated": []}
    if f:
        doc = read_json(settings.PANEL_OUT_DIR / "fundamentals.json", {}) or {}
        (doc.setdefault("items", {}))[code] = f
        atomic_write_json(settings.PANEL_OUT_DIR / "fundamentals.json", doc)
        out["updated"].append("fundamentals")
    if e:
        doc = read_json(settings.PANEL_OUT_DIR / "earnings.json", {}) or {}
        (doc.setdefault("items", {}))[code] = e
        atomic_write_json(settings.PANEL_OUT_DIR / "earnings.json", doc)
        out["updated"].append("earnings")
    if r:
        doc = read_json(settings.PANEL_OUT_DIR / "research.json", {}) or {}
        (doc.setdefault("items", {}))[code] = r
        atomic_write_json(settings.PANEL_OUT_DIR / "research.json", doc)
        out["updated"].append("research")
    if u:
        doc = read_json(settings.PANEL_OUT_DIR / "events.json", {}) or {}
        ups = [x for x in (doc.get("upcoming_earnings") or []) if x.get("code") != code]
        ups.append(u)
        ups.sort(key=lambda x: x["date"])
        doc["upcoming_earnings"] = ups
        atomic_write_json(settings.PANEL_OUT_DIR / "events.json", doc)
        out["updated"].append("upcoming")
    if news:
        doc = read_json(settings.PANEL_OUT_DIR / "analysis.json", {}) or {}
        old = [n for n in (doc.get("news") or []) if n.get("code") != code]
        mx = max([int(str(n.get("id", "n0")).lstrip("n") or 0) for n in old] or [0])
        for i, n in enumerate(news):
            n["id"] = f"n{mx + 1 + i}"
        doc["news"] = old + news
        atomic_write_json(settings.PANEL_OUT_DIR / "analysis.json", doc)
        out["updated"].append("news")
    return out
