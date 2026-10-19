"""本地研究子页只读 API；只消费 harness 自产 panel JSON 和本地 config。"""
from __future__ import annotations

import datetime as dt
import math
import re
from difflib import SequenceMatcher
from typing import Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import filings, settings
from .store import read_json

PANEL_FILES = frozenset({"analysis", "events", "earnings", "fundamentals", "research", "statements"})

_CODE = re.compile(r"^[A-Za-z0-9.]{1,20}$")
_PERIODIC = re.compile(
    r"定期报告|年度报告|中期报告|半年度报告|季度报告|年报|中报|季报|业绩公告|"
    r"年度业绩|中期业绩|季度业绩|止[三六九十]个?月|10-[KQ]\b|20-F\b|40-F\b",
    re.IGNORECASE,
)
_PERIODIC_TYPES = {"定期报告", "业绩公告", "年报", "中报", "季报", "10-K", "10-Q"}
_KIND_RANK = {"official": 3, "event": 2, "media": 1}


def _panel(name: str) -> dict:
    if name not in PANEL_FILES:
        raise ValueError("不允许读取此研究数据")
    data = read_json(settings.PANEL_OUT_DIR / f"{name}.json", {})
    return filings.normalize_panel_sources(name, data) if isinstance(data, dict) else {}


def _stocks() -> dict[str, dict]:
    data = read_json(settings.CONFIG_DIR / "holdings.json", {})
    if not isinstance(data, dict):
        return {}
    result = {}
    for row in (data.get("positions") or []) + (data.get("watch") or []):
        if isinstance(row, dict) and isinstance(row.get("code"), str):
            result[row["code"]] = row
    return result


def _stock(code: str) -> dict:
    if not _CODE.fullmatch(code):
        raise HTTPException(400, "bad code")
    stock = _stocks().get(code)
    if stock is None:
        raise HTTPException(404, "not_found")
    return stock


def _items(data: dict) -> dict:
    values = data.get("items")
    return values if isinstance(values, dict) else {}


def _rows(data: dict, key: str) -> list[dict]:
    values = data.get(key)
    return [row for row in values if isinstance(row, dict)] if isinstance(values, list) else []


def _day(value: Any) -> str | None:
    date = str(value or "")[:10]
    try:
        return dt.date.fromisoformat(date).isoformat()
    except ValueError:
        return None


def _positive(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    except (ValueError, TypeError):
        return None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def _today() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()


def _title_key(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", title).lower()


def _same_story(a: dict, b: dict) -> bool:
    if a.get("url") and a["url"] == b.get("url"):
        return True
    try:
        days = abs((dt.date.fromisoformat(a["date"]) - dt.date.fromisoformat(b["date"])).days)
    except ValueError:
        return False
    left, right = _title_key(a["title"]), _title_key(b["title"])
    return days <= 2 and min(len(left), len(right)) >= 8 and SequenceMatcher(None, left, right).ratio() >= .78


def timeline(code: str) -> dict:
    st = _stock(code)
    analysis, events, earnings = (_panel(x) for x in ("analysis", "events", "earnings"))
    raw = []
    for item in _rows(analysis, "news"):
        if item.get("code") != code or not (date := _day(item.get("time") or item.get("date"))):
            continue
        raw.append({"kind": "media", "code": code, "name": st.get("name"), "date": date,
                    "title": item.get("title") or "", "body": item.get("summary") or "",
                    "source": item.get("source") or "媒体（来源未注明）", "source_type": "secondary",
                    "verification": "非一手资讯，需核实公告原文", "url": item.get("url") or "",
                    "category": item.get("category") or "公司经营", "sentiment": item.get("sentiment") or "中性"})
    for item in _rows(events, "items"):
        if item.get("code") != code or not (date := _day(item.get("date"))):
            continue
        # events.items 混合媒体事件与公告分类，文件级 source 不足以证明单条是一手资料。
        raw.append({"kind": "event", "code": code, "name": st.get("name"), "date": date,
                    "title": item.get("title") or "", "body": item.get("desc") or "",
                    "source": "harness 事件归类（原始来源待核验）", "source_type": "secondary",
                    "verification": "不能仅凭事件归类视为交易所公告", "url": item.get("url") or "",
                    "category": item.get("category") or "公司经营", "type": item.get("type") or ""})
    er = _items(earnings).get(code) or {}
    for item in _rows(er, "announcements"):
        if item.get("type") in _PERIODIC_TYPES or _PERIODIC.search(str(item.get("title") or "")):
            continue  # 定期报告与业绩公告仅在财报页保留原文入口，不在动态页重复。
        if not (date := _day(item.get("date"))):
            continue
        url = str(item.get("url") or "")
        verified = filings.primary_link(url, code)
        raw.append({"kind": "official" if verified else "event", "code": code, "name": st.get("name"), "date": date,
                    "title": item.get("title") or "", "body": "",
                    "source": ("港交所披露易" if code.endswith(".HK") else "SEC EDGAR") if verified else "公告目录（原始文件待核验）",
                    "source_type": "primary" if verified else "secondary",
                    "verification": "公告链接可核对原文" if verified else "缺少已核验的一手公告链接",
                    "url": url, "category": item.get("type") or "公告", "type": item.get("type") or ""})
    raw.sort(key=lambda x: (x["date"], _KIND_RANK[x["kind"]]), reverse=True)
    groups: list[list[dict]] = []
    for item in raw:
        group = next((g for g in groups if any(_same_story(item, other) for other in g)), None)
        if group is None:
            groups.append([item])
        else:
            group.append(item)
    out = []
    for group in groups:
        group.sort(key=lambda x: (_KIND_RANK[x["kind"]], x["date"]), reverse=True)
        main = group[0]
        out.append({"id": f"{main['kind']}:{main['date']}:{main['title'][:16]}",
                    "date": max(x["date"] for x in group), "kind": main["kind"],
                    "kinds": list(dict.fromkeys(x["kind"] for x in group)),
                    "category": main["category"], "sentiment": main.get("sentiment") or "中性",
                    "main": main, "related": group[1:], "size": len(group),
                    "imp": None, "score": None})  # 未运行重要性模型，不能伪造规则评分。
    out.sort(key=lambda x: (x["date"], _KIND_RANK[x["kind"]]), reverse=True)
    today = _today()
    upcoming = []
    for row in _rows(events, "upcoming_earnings"):
        date = _day(row.get("date"))
        if row.get("code") != code or not date:
            continue
        days = (dt.date.fromisoformat(date) - dt.date.fromisoformat(today)).days
        if -3 <= days <= 90:
            note = str(row.get("note") or "")
            upcoming.append({"type": "earnings", "date": date, "days": days,
                             "title": "财报披露" + (f"（{row['period']}）" if row.get("period") else ""),
                             "note": note, "estimated": "估算" in note,
                             "source": "Finnhub 财报日历" if "Finnhub" in note else "harness 程序估算"})
    counts = {k: sum(x["kind"] == k for x in raw) for k in _KIND_RANK}
    return {"code": code, "today": today, "generated_at": {"news": analysis.get("generated_at"),
            "events": events.get("generated_at"), "announcements": earnings.get("generated_at")},
            "source": {"news": analysis.get("sources"), "events": events.get("source"), "announcements": earnings.get("sources")},
            "items": out, "upcoming": sorted(upcoming, key=lambda x: x["date"]),
            "stats": {"raw": len(raw), "clusters": len(out), "merged": len(raw) - len(out), "byKind": counts},
            "missing": (["没有可核验的近期一手公告；事件归类和媒体资讯不可充当官方披露"]
                        if not any(x["kind"] == "official" for x in raw) else [])}


def valuation(code: str) -> dict:
    st = _stock(code)
    data = _panel("fundamentals")
    item = _items(data).get(code) or {}
    val = item.get("valuation") if isinstance(item.get("valuation"), dict) else {}
    date = _day(val.get("date"))
    source = val.get("note") or data.get("sources")
    metrics = {}
    for alias, field in (("pe", "pe_ttm"), ("pb", "pb"), ("ps", "ps_ttm")):
        cur = _positive(val.get(field)) if date and source else None
        metrics[alias] = {"cur": cur, "rank": None, "band": None, "n": 0,
                          "source": source if cur is not None else None, "asof": date if cur is not None else None}
    peers = []
    # 只在明确标注同板块、同估值日期且有有效倍数时提供描述性横比；不推断行业中位数。
    if st.get("sector") and date:
        for other_code, other_st in _stocks().items():
            other = _items(data).get(other_code) or {}
            other_val = other.get("valuation") if isinstance(other.get("valuation"), dict) else {}
            if (other_code == code or other_st.get("sector") != st["sector"]
                    or other_st.get("market") != st.get("market") or _day(other_val.get("date")) != date
                    or not (other_val.get("note") or data.get("sources"))):
                continue
            pe, pb = _positive(other_val.get("pe_ttm")), _positive(other_val.get("pb"))
            if pe is not None or pb is not None:
                peers.append({"code": other_code, "name": other_st.get("name"), "pe": pe, "pb": pb,
                              "asof": date, "source": other_val.get("note") or data.get("sources")})
    missing = ["无历史估值与披露日对齐序列，无法计算分位、估值带或价格区间",
               "无现金流与资本开支完整序列，无法计算反向 DCF 或目标价"]
    if not date or not source or all(x["cur"] is None for x in metrics.values()):
        missing.insert(0, "harness 自产 fundamentals 未覆盖该标的有时点和来源的有效 PE/PB/PS")
    if not peers:
        missing.append("未找到同市场同板块、同估值日期且有来源的可比标的；不生成行业对比")
    return {"ok": True, "code": code, "name": st.get("name"), "asof": date,
            "generated_at": data.get("generated_at"), "source": data.get("sources"),
            "valuation": {"pe_ttm": metrics["pe"]["cur"], "pb": metrics["pb"]["cur"],
                          "ps_ttm": metrics["ps"]["cur"], "pe_forward": _positive(val.get("pe_forward")) if date and source else None,
                          "note": source if date else None},
            "hist_ok": False, "metrics": metrics, "series": [], "price_band": None,
            "dcf": None, "overall": None, "score": None, "primary": None,
            "peers": {"list": peers, "configured": len(peers), "median": None, "prem_pe": None, "prem_pb": None},
            "missing": missing}


def financial_statements(code: str) -> dict:
    st = _stock(code)
    obj = _panel("statements")
    item = _items(obj).get(code)
    if not code.endswith(".US"):
        reason = "港股尚无独立的标准科目链路；不能把 SEC 的美股/ADR 数据错配为港股报告"
    elif not isinstance(item, dict):
        reason = "尚未生成 SEC EDGAR 年度标准科目摘录；等待面板定时更新或运行 panel-build"
    elif item.get("status") == "unavailable":
        reason = "SEC 该标的未覆盖可识别的年度标准科目；不能用媒体摘录补全"
    else:
        reason = "仅展示当期可核对的标准 XBRL 重点科目，非完整三表；不完整科目不得用于 DCF"
    return {"ok": True, "code": code, "name": st.get("name"), "status": item.get("status") if isinstance(item, dict) else "unavailable",
            "item": item if isinstance(item, dict) and code.endswith(".US") else None,
            "source": "SEC EDGAR companyfacts" if code.endswith(".US") else None,
            "missing": [reason] + (item.get("missing", []) if isinstance(item, dict) and code.endswith(".US") else [])}


def register_research_routes(app: FastAPI) -> None:
    @app.get("/api/timeline")
    async def get_timeline(code: str = "") -> dict:
        return timeline(code)

    @app.get("/api/valuation")
    async def get_valuation(code: str = "", force: str = "") -> JSONResponse:
        """动态估值：westock 财报+周K 的分位带/反向DCF/可比公司（8899 契约）；
        westock CLI 缺失或标的无法映射时回退到 fundamentals 快照版（如实 missing）。"""
        from .providers import westock
        if not code or not _CODE.fullmatch(code):
            return JSONResponse({"ok": False, "error": "bad_code"}, status_code=400)
        st = _stock(code)
        from . import valuation as val_engine
        saved = val_engine.read_results().get(code)
        # 预计算结果优先（夜间 valuation-refresh）；手动「重新拉取」或无结果时才现算
        if saved and force != "1":
            return JSONResponse({**saved, "from_cache": True})
        if westock.available() and st.get("gtimg"):
            try:
                import asyncio
                d = await asyncio.to_thread(val_engine.view, code, force == "1")
                if d.get("ok") or d.get("error") == "insufficient":
                    val_engine.store_result(code, d)
                    return JSONResponse(d)
            except Exception as exc:
                if saved:  # 现算失败：回退上一次成功结果（last-good），页面标注计算日期
                    return JSONResponse({**saved, "from_cache": True, "stale": True, "refresh_error": str(exc)[:120]})
                return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=502)
        if saved:
            return JSONResponse({**saved, "from_cache": True})
        return JSONResponse(valuation(code))

    @app.get("/api/valuation/all")
    async def get_valuation_all() -> JSONResponse:
        """全组合估值结论精简版（决策页「贵不贵」与估值页同源）。"""
        from . import valuation as val_engine
        doc = val_engine.read_results()
        return JSONResponse({"items": {c: val_engine.compact(d) for c, d in doc.items()}})

    @app.get("/api/signals")
    async def get_signals(code: str = "", force: str = "") -> JSONResponse:
        """量化信号 v2：价格因子引擎（signals_engine）+ 仓位动作矩阵 + 账本回测。"""
        from . import signals_service
        if code and not _CODE.fullmatch(code):
            return JSONResponse({"ok": False, "error": "bad_code"}, status_code=400)
        try:
            d = await signals_service.build_signals(force == "1")
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=502)
        if code:
            item = next((x for x in d.get("items", []) if x.get("code") == code), None)
            if not item:
                return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
            return JSONResponse({"ok": True, "at": d.get("at"), "weights": d.get("weights"),
                                 "summary": d.get("summary"), "item": item})
        return JSONResponse(d)

    @app.get("/api/financial/statements")
    async def get_financial_statements(code: str = "") -> dict:
        return financial_statements(code)
