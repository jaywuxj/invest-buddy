"""组合、交易与预警写端点；复用既有的 GET holdings/alerts，不导入外部面板产物。"""
from __future__ import annotations

import hashlib
import asyncio
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import holdings_service as svc, portfolio, settings
from .alerts import evaluate_rules
from .providers import GtimgQuoteProvider
from .store import read_json

_TZ = timezone(timedelta(hours=8))
_PROVIDER = GtimgQuoteProvider()
_SEARCH_RE = re.compile(r'v_hint="([^"]*)"')
_RULE_LIMITS = {
    "price_below": (0.0001, 1e7), "price_above": (0.0001, 1e7),
    "day_change_abs": (0.5, 50), "pe_below": (0.1, 500),
}
_ALERT_TYPES = {k: {"label": label} for k, label in (
    ("price_below", "跌破价格"), ("price_above", "涨破价格"),
    ("day_change_abs", "单日涨跌幅"), ("pe_below", "市盈率低于"),
)}
_DEFAULT_RULES = {"single_max": 25, "single_warn": 20, "top3_max": 50, "sector_max": 35,
                  "market_max": 70, "stop_review_pct": -50, "deep_loss_pct": -40,
                  "deep_loss_count_max": 3, "portfolio_loss_max": -20,
                  "loss_tiers": [0, -20, -40, -50], "fx_shock_pct": 5, "sector_groups": {}}


def _at() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _error(exc: svc.PortfolioError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": exc.error, "message": exc.message}, status_code=400)


async def _quotes_and_fx(codes: list[str]) -> tuple[dict, dict, str, str | None]:
    """一笔腾讯行情请求同时取股票与实时汇率；未拿到汇率不伪造 CNY 换算。"""
    req = list(dict.fromkeys(codes + ["whHKDCNY", "whUSDCNY"]))
    try:
        raw = await _PROVIDER.get_quotes(req)
        err = None
    except Exception as exc:
        raw, err = {}, str(exc)
    rates = {"CNY": 1.0}
    for currency, key in (("HKD", "whHKDCNY"), ("USD", "whUSDCNY")):
        price = (raw.get(key) or {}).get("price")
        if isinstance(price, (float, int)) and math.isfinite(price) and price > 0:
            rates[currency] = price
    return raw, rates, "gtimg", err


async def stock_search(q: str = "") -> dict:
    q = q.strip()[:30]
    if not q:
        return {"ok": True, "items": []}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get("https://smartbox.gtimg.cn/s3/", params={"v": "2", "t": "all", "q": q},
                                    headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            text = resp.content.decode("gbk", errors="replace")
    except httpx.HTTPError:
        raise HTTPException(502, detail={"ok": False, "error": "stock_search_unavailable"}) from None
    match = _SEARCH_RE.search(text)
    h = portfolio.load_holdings()
    held = {s["code"]: "pos" for s in h.get("positions", [])}
    held.update({s["code"]: "watch" for s in h.get("watch", [])})
    items, seen = [], set()
    for row in (match.group(1) if match else "").split("^"):
        fields = row.split("~")
        if len(fields) < 5 or not fields[4].startswith(("GP", "ETF", "QDII-ETF", "LOF", "KJ", "JJ")):
            continue
        mk, symbol = fields[0].lower(), fields[1].upper()
        if mk not in ("hk", "us", "sh", "sz", "bj"):
            continue
        if mk == "us":
            symbol, _, exchange = symbol.partition(".")
            if exchange == "PS":
                continue
        else:
            exchange = ""
            if mk == "hk":
                symbol = symbol.zfill(5)
        try:
            ident = svc.identity(f"{symbol}.{mk.upper()}")
        except svc.PortfolioError:
            continue
        if ident["code"] in seen:
            continue
        seen.add(ident["code"])
        name = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), fields[2]).strip()[:30]
        item = {**ident, "name": name or ident["code"], "kind": fields[4], "in": held.get(ident["code"])}
        if mk == "us" and exchange in ("N", "AM", "OQ"):
            item["kline"] = "us" + symbol + "." + exchange
        items.append(item)
        if len(items) == 10:
            break
    return {"ok": True, "items": items}


def _rules() -> dict:
    data = read_json(settings.CONFIG_DIR / "portfolio_rules.json", {}) or {}
    return {**_DEFAULT_RULES, **data}


async def portfolio_diagnosis() -> dict:
    h = portfolio.load_holdings()
    positions = [p for p in h.get("positions", []) if isinstance(p.get("shares"), (int, float))
                 and isinstance(p.get("cost"), (int, float)) and p["shares"] > 0 and p["cost"] > 0]
    raw, rates, source, error = await _quotes_and_fx([p["gtimg"] for p in positions if p.get("gtimg")])
    live = (read_json(settings.DATA_DIR / "live.json", {}) or {}).get("quotes", {})
    rules = _rules()
    rows, missing, missing_fx = [], [], []
    for p in positions:
        code = p["code"]
        currency = p.get("currency", "CNY")
        if currency not in rates:
            missing_fx.append(code)
            continue
        q = raw.get(p.get("gtimg")) or {}
        price, pct, src = q.get("price"), q.get("pct"), "gtimg"
        if price is None and (live.get(code) or {}).get("price") is not None:
            price, pct, src = live[code]["price"], live[code].get("pct"), "live"
        if price is None:
            price, pct, src = p["cost"], None, "cost"
            missing.append(p.get("name", code))
        rate = rates[currency]
        mv, cost = price * p["shares"] * rate, p["cost"] * p["shares"] * rate
        day = mv - mv / (1 + pct / 100) if isinstance(pct, (int, float)) and pct > -100 else 0
        sector = p.get("sector") or "其他"
        rows.append({"code": code, "name": p.get("name", code), "market": p.get("market", ""),
                     "currency": currency, "sector": sector,
                     "group": rules["sector_groups"].get(sector, sector),
                     "price": price, "pct": pct, "price_src": src, "mv": mv, "cost": cost,
                     "pnl": mv - cost, "pnl_pct": (mv / cost - 1) * 100,
                     "day_pnl": day})
    total_mv = sum(r["mv"] for r in rows)
    total_cost = sum(r["cost"] for r in rows)
    # 现金与已实现盈亏（2026-10-20）：仓位分母改为总资产（持仓市值 + 现金）
    from . import cash as cash_mod
    try:
        funds = cash_mod.funds(total_mv, total_mv - total_cost, rates)
    except Exception as exc:                      # 现金文件损坏时不能拖垮组合页，如实标注
        funds = {"cash_cny": 0.0, "realized_cny": 0.0, "total_assets": total_mv,
                 "total_return_cny": total_mv - total_cost, "net_invested_cny": total_cost,
                 "net_invested_used": total_cost, "return_pct": None, "cash_weight": 0.0,
                 "cash_by_currency": [], "configured": False, "error": str(exc)[:160]}
    total_assets = float(funds.get("total_assets") or 0) or total_mv
    denom = total_assets                           # 现金为 0 时等于 total_mv，旧结论不变
    rows.sort(key=lambda r: r["mv"], reverse=True)
    for row in rows:
        row["weight"] = row["mv"] / denom * 100 if denom else 0

    def group(key: str) -> list[dict]:
        out: dict[str, dict] = {}
        for r in rows:
            name = r[key]
            item = out.setdefault(name, {"name": name, "mv": 0, "pnl": 0, "members": []})
            item["mv"] += r["mv"]
            item["pnl"] += r["pnl"]
            item["members"].append(r["name"])
        for item in out.values():
            item["weight"] = item["mv"] / denom * 100 if denom else 0
        return sorted(out.values(), key=lambda a: a["mv"], reverse=True)

    by_sector, by_market, by_currency = group("group"), group("market"), group("currency")
    themes = (read_json(settings.PANEL_OUT_DIR / "themes.json", {}) or {}).get("items", [])
    by_theme = []
    for theme in themes if isinstance(themes, list) else []:
        members = [r for r in rows if r["code"] in theme.get("members", [])]
        if members:
            mv = sum(r["mv"] for r in members)
            by_theme.append({"name": theme.get("name"), "mv": mv,
                             "weight": mv / denom * 100 if denom else 0,
                             "members": [r["name"] for r in members]})
    by_theme.sort(key=lambda t: t["mv"], reverse=True)
    tiers = sorted(rules["loss_tiers"], reverse=True)
    loss_tiers = []
    for i in range(len(tiers) + 1):
        bucket = [r for r in rows if sum(r["pnl_pct"] < t if t == 0 else r["pnl_pct"] <= t for t in tiers) == i]
        loss_tiers.append({"label": "盈利" if i == 0 else (f"≤{tiers[-1]}%" if i == len(tiers) else f"{tiers[i - 1]}~{tiers[i]}%"),
                           "level": i, "count": len(bucket), "weight": sum(r["weight"] for r in bucket),
                           "pnl": sum(r["pnl"] for r in bucket),
                           "members": [{"name": r["name"], "pnl_pct": round(r["pnl_pct"], 1)} for r in bucket]})
    breaches: list[dict] = []

    def breach(key: str, level: str, dim: str, title: str, value: float, limit: float):
        breaches.append({"key": key, "level": level, "dim": dim, "title": title,
                         "detail": title, "value": round(value, 1), "limit": limit})

    for row in rows:
        if row["weight"] > rules["single_max"]:
            breach("single:" + row["code"], "red", "单股集中度", row["name"] + " 仓位超过单股红线", row["weight"], rules["single_max"])
        elif row["weight"] > rules["single_warn"]:
            breach("single:" + row["code"], "amber", "单股集中度", row["name"] + " 接近单股红线", row["weight"], rules["single_max"])
        if row["pnl_pct"] <= rules["stop_review_pct"]:
            breach("stop:" + row["code"], "red", "止损审视", row["name"] + " 浮亏达到审视线", row["pnl_pct"], rules["stop_review_pct"])
    top3 = sum(r["weight"] for r in rows[:3])
    if top3 > rules["top3_max"]:
        breach("top3", "amber", "头部集中度", "前三大持仓占比超过红线", top3, rules["top3_max"])
    for item, field, level in ((by_sector, "sector_max", "red"), (by_market, "market_max", "amber")):
        for g in item:
            if g["weight"] > rules[field]:
                breach(field + ":" + g["name"], level, "行业集中度" if field == "sector_max" else "市场集中度",
                       g["name"] + " 敞口过高", g["weight"], rules[field])
    deep = [r for r in rows if r["pnl_pct"] <= rules["deep_loss_pct"]]
    if len(deep) >= rules["deep_loss_count_max"]:
        breach("deep_loss", "amber", "深度浮亏", "深度浮亏持仓达到警戒数量", len(deep), rules["deep_loss_count_max"])
    pnl_pct = (total_mv / total_cost - 1) * 100 if total_cost else 0
    if rows and pnl_pct <= rules["portfolio_loss_max"]:
        breach("portfolio_loss", "red", "组合回撤", "组合浮亏触及警戒线", pnl_pct, rules["portfolio_loss_max"])
    breaches.sort(key=lambda x: x["level"] != "red")
    fx_sensitivity = [{"currency": g["name"], "mv": g["mv"], "weight": g["weight"],
                       "impact": g["mv"] * rules["fx_shock_pct"] / 100} for g in by_currency if g["name"] != "CNY"]
    return {"ok": True, "at": _at(), "fx": {"rates": rates, "source": source, "error": error},
            "totals": {"mv": total_mv, "cost": total_cost, "pnl": total_mv - total_cost,
                       "pnl_pct": pnl_pct, "day_pnl": sum(r["day_pnl"] for r in rows), "count": len(rows),
                       "hhi_n": 1 / sum((r["weight"] / 100) ** 2 for r in rows) if rows else 0,
                       "cash": funds.get("cash_cny"), "total_assets": total_assets,
                       "cash_weight": funds.get("cash_weight"),
                       "realized_cny": funds.get("realized_cny"),
                       "total_return_cny": funds.get("total_return_cny"),
                       "net_invested_cny": funds.get("net_invested_used"),
                       "return_pct": funds.get("return_pct")},
            "funds": funds,
            "rows": rows, "by_sector": by_sector, "by_market": by_market, "by_currency": by_currency,
            "by_theme": by_theme, "loss_tiers": loss_tiers, "fx_sensitivity": fx_sensitivity,
            "breaches": breaches,
            "summary": {"red": sum(b["level"] == "red" for b in breaches),
                        "amber": sum(b["level"] == "amber" for b in breaches)},
            "rules": {k: rules[k] for k in ("single_max", "sector_max", "market_max", "top3_max", "fx_shock_pct")},
            "missing": missing, "missing_fx": missing_fx,
            "partial": bool(missing_fx)}


async def trade_view() -> dict:
    h, d, by = svc.trade_snapshot()
    raw, rates, source, err = await _quotes_and_fx([s["gtimg"] for s in h["positions"] + h["watch"] if s.get("gtimg")])
    ent = {s["code"]: s for s in h["positions"] + h["watch"]}
    rows, drift = [], []
    for p in h["positions"]:
        s = by.get(p["code"])
        if s and (abs(s["shares"] - (p.get("shares") or 0)) > 1e-6 or abs((s["cost"] or 0) - (p.get("cost") or 0)) > .01):
            drift.append(p["code"])
    for code, state in by.items():
        st = ent.get(code, {})
        currency = state["currency"]
        rate = rates.get(currency)
        price = (raw.get(st.get("gtimg")) or {}).get("price")
        held = state["shares"] > 0
        if price is None and held:
            price = state["cost"]
        trades = [t for t in d["items"] if t["code"] == code]
        local_pnl = (state["shares"] * price if price is not None else 0) + sum(
            -(t["shares"] * t["price"] + t.get("fee", 0)) if t["side"] in ("init", "buy") else
            (t["shares"] * t["price"] - t.get("fee", 0)) if t["side"] == "sell" else
            (t["amount"] - t.get("fee", 0)) if t["side"] == "dividend" else 0 for t in trades)
        invested = sum(t["shares"] * t["price"] for t in trades if t["side"] in ("init", "buy"))
        if st.get("gtimg"):  # 现价并入个股日 K（当日可能未收盘，日 K 无当日数据）
            import datetime as _dt
            _today = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date().isoformat()
        rows.append({"code": code, "name": state["name"], "currency": currency,
                     "shares": state["shares"], "cost": round(state["cost"], 2) if state["cost"] else None,
                     "price": round(price, 2) if price is not None else None, "held": held,
                     "price_src": "gtimg" if st.get("gtimg") in raw else "cost" if held else "none",
                     "mv_cny": round(state["shares"] * price * rate, 2) if price is not None and rate else None,
                     "invested_cny": round(invested * rate, 2) if rate else None,
                     "realized_cny": round(state["realized"] * rate, 2) if rate else None,
                     "dividends_cny": round(state["dividends"] * rate, 2) if rate else None,
                     "pnl_cny": round(local_pnl * rate, 2) if rate else None,
                     "pnl_pct": round(local_pnl / invested * 100, 2) if invested and price is not None else None,
                     "market_cny": None, "selection_cny": None, "timing_cny": None, "fx_cny": None,
                     "bench": None, "bench_missing": len(trades), "trades": len(trades),
                     "last_date": state["last_date"], "error": None})
    rows.sort(key=lambda r: r["mv_cny"] or 0, reverse=True)
    sums = {key: round(sum(r[key] or 0 for r in rows), 2)
            for key in ("pnl_cny", "realized_cny", "dividends_cny", "mv_cny")}
    sums.update(market_cny=None, selection_cny=None, timing_cny=None, fx_cny=None)
    # 现金与总收益（2026-10-20）：pnl_cny 已含浮动＋交易流水已实现，此处只补历史一次性录入
    from . import cash as cash_mod
    try:
        f = cash_mod.compose(sums.get("mv_cny") or 0,
                             (sums.get("pnl_cny") or 0) + (cash_mod.summary(rates).get("realized_history_cny") or 0),
                             rates, with_nav=False)
        sums.update(cash_cny=f["cash_cny"], total_assets_cny=f["total_assets"],
                    realized_history_cny=f["realized_history_cny"], total_return_cny=f["total_return_cny"],
                    unrealized_cny=f["unrealized_cny"], net_invested_cny=f["net_invested_used"],
                    return_pct=f["return_pct"])
        funds_extra = {"funds": f}
    except Exception as exc:
        funds_extra = {"funds_error": str(exc)[:160]}
    # 四项归因（2026-10-16）：市场/汇率/时机/选股，四项严格加总当前持仓浮动盈亏；按天缓存
    from . import attribution
    try:
        att = await asyncio.to_thread(attribution.compute_cached, h, d["items"], by)
        if att.get("rows"):
            amap = {x["code"]: x for x in att["rows"]}
            for r in rows:
                a2 = amap.get(r["code"])
                if a2:
                    r.update(market_cny=a2["market_cny"], selection_cny=a2["selection_cny"],
                             timing_cny=a2["timing_cny"], fx_cny=a2["fx_cny"])
                    r["bench"] = att["meta"]["bench"].get(r["currency"])
                    r["bench_missing"] = 0
            sums.update(market_cny=att["totals"].get("market_cny"), selection_cny=att["totals"].get("selection_cny"),
                        timing_cny=att["totals"].get("timing_cny"), fx_cny=att["totals"].get("fx_cny"))
        attr_extra = {"att_meta": att.get("meta"), "att_cached": bool(att.get("cached")), "att_stale": bool(att.get("stale"))}
    except Exception as exc:
        attr_extra = {"att_error": str(exc)[:120]}
    return {"ok": True, "updated_at": d.get("updated_at"), "sides": svc._SIDES,
            "sell_reasons": svc._SELL_REASONS, "items": list(reversed(d["items"])),
            "attribution": {"rows": rows, "totals": sums, "drift": drift,
                            "fx": rates, "fx_source": source, "fx_error": err, "partial": any(s["currency"] not in rates for s in by.values()),
                            "available": True, **attr_extra, **funds_extra},
            "note": "平均成本、已实现收益及分红照常计算；市场/汇率/时机/选股归因：四项加总=当前持仓浮动盈亏，已卖出部分在「已实现/分红」列；成交日行情/汇率取当日收盘，误差约一个交易日。"}


def _alert_id(rule: dict) -> str:
    # value_ref 解析出的现值与 note 会随研报变化，不应改变旧规则的身份。
    key = (rule.get("code"), rule.get("type"), rule.get("value_ref"),
           None if rule.get("value_ref") else rule.get("value"))
    return "a" + hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:16]


def _alert_rules(data: dict) -> list[dict]:
    rules = data.get("rules", [])
    used = {r["id"] for r in rules if r.get("id")}
    for rule in rules:
        if not rule.get("id"):
            base = _alert_id(rule)
            candidate = base
            ordinal = 2
            while candidate in used:
                candidate = f"{base}-{ordinal}"
                ordinal += 1
            rule["id"] = candidate
            used.add(candidate)
    return rules


def mutate_alerts(body: dict) -> dict:
    with svc._write_lock():
        file = settings.CONFIG_DIR / "alerts.json"
        if file.is_symlink():
            raise svc.PortfolioError("corrupt_data", "alerts.json 是符号链接；已停止写入并保留原件")
        data = svc._strict_json(file, ("rules",))
        rules = _alert_rules(data)
        op = body.get("op")
        if op in ("add", "update"):
            if op == "add" and len(rules) >= 200:
                raise svc.PortfolioError("limit_exceeded", "最多 200 条预警规则")
            base = {} if op == "add" else next((x for x in rules if x["id"] == body.get("id")), None)
            if base is None:
                raise svc.PortfolioError("rule_not_found")
            code = body.get("code", base.get("code"))
            ident = svc.identity(code)
            stock = portfolio.stock_meta(ident["code"])
            if stock is None or stock.get("gtimg") != ident["gtimg"]:
                raise svc.PortfolioError("unknown_code", "标的不在持仓或关注列表")
            kind = body.get("type", base.get("type"))
            if kind not in _RULE_LIMITS:
                raise svc.PortfolioError("unsupported_rule", "当前仅支持价格、日涨跌幅与市盈率预警")
            low, high = _RULE_LIMITS[kind]
            value = svc._number(body.get("value", base.get("value")), "预警阈值")
            if not low <= value <= high:
                raise svc.PortfolioError("bad_value", f"阈值需在 {low} 至 {high} 范围内")
            rule = {**base, "code": ident["code"], "gtimg": ident["gtimg"],
                    "name": stock.get("name", ident["code"]), "type": kind, "value": round(value, 4),
                    "active": body.get("active", base.get("active", True)) is True}
            rule.pop("value_ref", None)
            if "note" in body:
                rule["note"] = svc._text(body["note"], 200)
            if op == "add":
                rule["id"] = "a" + uuid.uuid4().hex[:16]
                rule["created_at"] = _at()
                rules.append(rule)
            else:
                rule["updated_at"] = _at()
                rules[rules.index(base)] = rule
        elif op in ("delete", "toggle"):
            rule = next((x for x in rules if x.get("id") == body.get("id")), None)
            if rule is None:
                raise svc.PortfolioError("rule_not_found")
            if op == "delete":
                rules.remove(rule)
            else:
                rule["active"] = body.get("active") is True if "active" in body else not rule.get("active", True)
        else:
            raise svc.PortfolioError("bad_op", "不支持的预警操作")
        data["updated_at"] = datetime.now(_TZ).date().isoformat()
        svc._write(file, data)
        return {"ok": True, "rules": rules}


async def alert_preview() -> dict:
    cfg = portfolio.resolved_alert_config()
    rules = _alert_rules(cfg)
    raw, _, _, fetch_error = await _quotes_and_fx([r.get("gtimg", "") for r in rules if r.get("gtimg")])
    hits = evaluate_rules([r for r in rules if r.get("type") in _RULE_LIMITS and r.get("value") is not None], raw, {})
    matched = {(h["code"], h["type"], h["threshold"]) for h in hits}
    evals = {}
    for r in rules:
        q = raw.get(r.get("gtimg")) or {}
        kind, val = r.get("type"), r.get("value")
        cur, err = None, None
        if kind not in ("price_below", "price_above", "day_change_abs", "pe_below"):
            err = "当前预览不支持该规则类型"
        elif val is None:
            err = r.get("note", "目标价暂无数据")
        elif q.get("price") is None:
            err = fetch_error or "无实时行情"
        elif kind in ("price_below", "price_above"):
            cur = f"现价 {q['price']}，距触发 {(q['price'] / val - 1) * 100:+.1f}%"
        elif kind == "day_change_abs":
            cur = f"今日 {q.get('pct'):+.2f}%" if q.get("pct") is not None else "今日涨跌幅暂无数据"
        elif isinstance(q.get("pe"), (int, float)) and not isinstance(q["pe"], bool) and math.isfinite(q["pe"]) and q["pe"] > 0:
            cur = f"当前 PE {q['pe']}"
        else:
            err = "行情源无有效市盈率"
        hit = (r.get("code"), kind, val) in matched
        evals[r["id"]] = {"cur": cur, "err": err, "hit": bool(hit and r.get("active", True)),
                           "text": f"{_ALERT_TYPES.get(kind, {}).get('label', kind)} {val if val is not None else r.get('value_ref', '')}"}
    return {"at": _at(), "evals": evals, "types": _ALERT_TYPES}


def register_portfolio_routes(app: FastAPI) -> None:
    @app.post("/api/holdings")
    async def post_holdings(body: dict[str, Any]) -> dict:
        try:
            if body.get("op") in ("add_pos", "add_watch"):
                ident = svc.identity(body.get("code"), body.get("gtimg"))
                if not portfolio.stock_meta(ident["code"]):
                    try:
                        quotes = await _PROVIDER.get_quotes([ident["gtimg"]])
                    except Exception:
                        quotes = None  # 上游暂不可达；代码与 gtimg 的严格对应关系仍已校验。
                    if quotes is not None and not (quotes.get(ident["gtimg"]) or {}).get("price"):
                        raise svc.PortfolioError("unknown_code", "行情源查不到该标的")
            return svc.mutate_holdings(body)
        except svc.PortfolioError as exc:
            return _error(exc)

    _ONE_REFRESH: dict[str, dict] = {}

    @app.post("/api/refresh/stock")
    async def post_refresh_stock(body: dict[str, Any]) -> dict:
        """新增股票后立即补齐该股全部信息（程序数据 → LLM 内容）；同步串行执行，前端显示进度。"""
        from . import panel_data
        from .jobs import panel_build
        code = str(body.get("code") or "")
        import re as _re
        if not _re.fullmatch(r"[0-9A-Za-z.]{1,20}", code):
            return {"ok": False, "error": "bad code"}
        if _ONE_REFRESH.get(code, {}).get("running"):
            return {"ok": True, "running": True, **_ONE_REFRESH[code]}
        _ONE_REFRESH[code] = {"running": True}
        try:
            prog = await panel_data.refresh_one_stock(code)
            _ONE_REFRESH[code]["program"] = prog
            llm = await panel_build.refresh_llm_one(code)
            _ONE_REFRESH[code] = {"running": False, "program": prog, "llm": llm.get("steps"), "at": _at()}
            return {"ok": True, **_ONE_REFRESH[code]}
        except Exception as exc:
            _ONE_REFRESH[code] = {"running": False, "error": str(exc)[:160], "at": _at()}
            return {"ok": False, "error": str(exc)[:160]}

    @app.get("/api/stock_search")
    async def get_stock_search(q: str = "") -> dict:
        return await stock_search(q)

    @app.get("/api/trades")
    async def get_trades():
        try:
            return await trade_view()
        except svc.PortfolioError as exc:
            return _error(exc)

    @app.post("/api/trades")
    async def post_trades(body: dict[str, Any]) -> dict:
        try:
            svc.mutate_trades(body)
        except svc.PortfolioError as exc:
            return _error(exc)
        return await trade_view()

    @app.get("/api/portfolio")
    async def get_portfolio() -> dict:
        return await portfolio_diagnosis()

    @app.post("/api/alerts")
    async def post_alerts(body: dict[str, Any]) -> dict:
        try:
            return mutate_alerts(body)
        except svc.PortfolioError as exc:
            return _error(exc)

    @app.get("/api/cash")
    async def get_cash():
        """现金与资金总账：现金明细、已实现（流水+历史）、总资产、净投入、收益率。"""
        from . import cash as cash_mod
        try:
            _, rates, _, _ = await _quotes_and_fx([])
            total = await portfolio_diagnosis()
            t = total.get("totals") or {}
            return {"ok": True, "fx": rates,
                    **cash_mod.funds(float(t.get("mv") or 0), float(t.get("pnl") or 0), rates, with_nav=False)}
        except Exception as exc:
            raise HTTPException(500, detail={"ok": False, "error": "cash_unavailable", "message": str(exc)[:200]}) from None

    @app.post("/api/cash")
    async def post_cash(body: dict[str, Any]) -> dict:
        from . import cash as cash_mod
        try:
            res = cash_mod.mutate(body)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": "bad_request", "message": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": "cash_write_failed", "message": str(exc)[:200]}, status_code=500)
        _, rates, _, _ = await _quotes_and_fx([])
        total = await portfolio_diagnosis()
        t = total.get("totals") or {}
        return {"ok": True, "result": res,
                **cash_mod.funds(float(t.get("mv") or 0), float(t.get("pnl") or 0), rates, with_nav=False)}

    @app.get("/api/alerts/preview")
    async def get_alert_preview() -> dict:
        return await alert_preview()

    @app.post("/api/alerts/check")
    async def post_alert_check() -> dict:
        from .jobs.alert_check import run
        try:
            result = await run()
            unsupported = [r.get("id") or _alert_id(r) for r in portfolio.resolved_alert_config().get("rules", [])
                           if r.get("active", True) and r.get("type") not in ("price_below", "price_above", "day_change_abs", "pe_below")]
            return {**result, "partial": bool(unsupported), "unsupported_rules": unsupported}
        except Exception as exc:
            raise HTTPException(502, detail={"ok": False, "error": "alert_check_unavailable", "message": str(exc)}) from None
