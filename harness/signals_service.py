"""量化信号 v2 服务（对齐 8899 buildSignalsV2）：价格因子 + 仓位动作矩阵 + 信号账本。

数据：gtimg 日 K（kline.py）、组合诊断（portfolio_routes）、逻辑卡评估（theses.py）。
缓存 10 分钟；每次构建把当日信号写入账本（data/panel/signal_ledger.json）。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from . import portfolio, signals_engine as SIG, theses
from .app_settings import read_rules
from .kline import bj_date, bj_full, fetch_kline, kline_sym_of
from .panel_api import _gtimg  # 复用同一 gtimg 提供器（含熔断与缓存）
from .portfolio_routes import portfolio_diagnosis

_TTL_SEC = 600
_cache: tuple[float, dict] = (0.0, {})


def _fwd_ret(rows: list[dict], from_date: str, n: int) -> float | None:
    i = next((i for i, r in enumerate(rows) if r["d"] >= from_date), -1)
    if i < 0 or i + n >= len(rows):
        return None
    return (rows[i + n]["c"] / rows[i]["c"] - 1) * 100


def _tally(pairs: list[dict]) -> dict:
    n = hit = 0
    total = 0.0
    for p in pairs:
        if p.get("ret") is None or p.get("label") == "中性":
            continue
        n += 1
        ok = p["ret"] > 0 if p["label"] == "看多" else p["ret"] < 0
        hit += 1 if ok else 0
        total += p["ret"] if p["label"] == "看多" else -p["ret"]
    return {"n": n, "hit": hit, "rate": round(hit / n * 100, 1) if n else None,
            "edge": round(total / n, 2) if n else None}


def _backtest(rows: list[dict]) -> dict:
    closes = [x["c"] for x in rows]
    out = {"h20": [], "h60": []}
    start = max(120, len(closes) - 250 - 60)
    for t in range(start, len(closes)):
        f = SIG.price_factors(closes, t)
        ws = ss = 0.0
        for k in ("trend", "momentum", "position"):
            if k in f:
                ws += SIG.W[k]
                ss += SIG.W[k] * f[k]["s"]
        if not ws:
            continue
        d = ss / ws
        label = "看多" if d >= SIG.TH else "看空" if d <= -SIG.TH else "中性"
        if t + 20 < len(closes):
            out["h20"].append({"label": label, "ret": (closes[t + 20] / closes[t] - 1) * 100})
        if t + 60 < len(closes):
            out["h60"].append({"label": label, "ret": (closes[t + 60] / closes[t] - 1) * 100})
    return {"h20": _tally(out["h20"]), "h60": _tally(out["h60"])}


def _indep_check(rows: list[dict]) -> dict:
    """方法检验（诚实口径）：每隔 h 天取一个点（独立样本、不重叠），收益扣掉该股同期全部日子的平均收益（基准）。
    回答「价格方向信号出现后，比随便哪天买入多赚了多少」。只做全组合汇总，单股样本太少不展示。"""
    closes = [x["c"] for x in rows]
    out: dict[str, dict[str, list[float]]] = {}
    for h in (20, 60):
        start = max(120, len(closes) - 250 - h)
        rets = [(closes[t + h] / closes[t] - 1) * 100 for t in range(start, len(closes) - h)]
        if not rets:
            continue
        base = sum(rets) / len(rets)
        bull, bear = [], []
        for t in range(start, len(closes) - h, h):
            f = SIG.price_factors(closes, t)
            ws = sum(SIG.W[k] for k in f)
            if not ws:
                continue
            d = sum(SIG.W[k] * f[k]["s"] for k in f) / ws
            ex = (closes[t + h] / closes[t] - 1) * 100 - base
            if d >= SIG.TH:
                bull.append(ex)
            elif d <= -SIG.TH:
                bear.append(ex)
        out[f"h{h}"] = {"bull": bull, "bear": bear}
    return out


def _agg_check(per: list[dict]) -> dict:
    import statistics as stt
    res = {}
    for h in ("h20", "h60"):
        side = {}
        for k in ("bull", "bear"):
            xs = [v for x in per for v in ((x or {}).get(h) or {}).get(k, [])]
            if len(xs) >= 2:
                m, se = stt.mean(xs), stt.stdev(xs) / len(xs) ** 0.5
                side[k] = {"n": len(xs), "mean": round(m, 2), "se": round(se, 2)}
            else:
                side[k] = {"n": len(xs), "mean": None, "se": None}
        b, r = side["bull"], side["bear"]
        spread = significant = None
        if b["mean"] is not None and r["mean"] is not None:
            spread = round(b["mean"] - r["mean"], 2)  # 有效信号应为正：看多后跑赢、看空后跑输
            se = (b["se"] ** 2 + r["se"] ** 2) ** 0.5
            significant = bool(se and spread > 2 * se)
        res[h] = {**side, "spread": spread, "significant": significant}
    return res


async def build_signals(force: bool = False, only: str | None = None) -> dict:
    global _cache
    if not force and _cache[1] and time.time() - _cache[0] < _TTL_SEC:
        return _cache[1]
    h = portfolio.load_holdings()
    rules = read_rules()
    today = bj_date()
    try:
        diag = await portfolio_diagnosis()
    except Exception:
        diag = None
    row_of = {r["code"]: r for r in ((diag or {}).get("rows") or [])}
    try:
        tv = await theses.theses_view()
    except Exception:
        tv = {"items": {}}
    all_stocks = [{**x, "held": True} for x in (h.get("positions") or [])] + [
        {**x, "held": False} for x in (h.get("watch") or [])]
    if only:  # 单股刷新：只算该股，不写全量缓存
        all_stocks = [s for s in all_stocks if s.get("code") == only]
    quotes: dict[str, Any] = {}
    try:
        quotes = await _gtimg.get_quotes([s["gtimg"] for s in all_stocks if s.get("gtimg")])
    except Exception:
        pass
    ledger = SIG.read_ledger()

    async def one(st: dict) -> dict:
        bars: list[dict] = []
        try:
            bars = await asyncio.to_thread(fetch_kline, kline_sym_of(st.get("gtimg", ""), st), 640)
        except Exception:
            pass
        m = SIG.market_signal(bars)  # bars 为空时输出中性骨架（因子 s=null），不编造
        q = quotes.get(st.get("gtimg") or "")
        if q and q.get("price") is not None:
            m["close"] = q["price"]
        r = row_of.get(st["code"])
        th = (tv.get("items") or {}).get(st["code"])
        ev = (th or {}).get("eval")
        held = bool(st.get("held") and r)
        ctx = {"held": held, "weight": (r or {}).get("weight") or 0,
               "pnl_pct": (r or {}).get("pnl_pct"), "falsified": (ev or {}).get("falsified") or 0,
               "margin_ok": (ev or {}).get("margin_ok"), "has_thesis": bool(th)}
        pa = SIG.position_action(m, ctx, rules)
        mine = [e for e in ledger["entries"]
                if e.get("code") == st["code"] and e.get("kline_date") and e.get("model") == SIG.MODEL]
        live = {"h20": _tally([{"label": e["label"], "ret": _fwd_ret(bars, e["kline_date"], 20)} for e in mine]),
                "h60": _tally([{"label": e["label"], "ret": _fwd_ret(bars, e["kline_date"], 60)} for e in mine]),
                "n": len(mine)}
        bt = _backtest(bars) if len(bars) > 200 else None
        chk = _indep_check(bars) if len(bars) > 200 else None
        return {"code": st["code"], "name": st.get("name"), "held": held, "sector": st.get("sector") or "",
                **m,
                "position": {"weight": round(r["weight"], 2) if r else None,
                             "pnl_pct": round(r["pnl_pct"], 2) if r and r.get("pnl_pct") is not None else None,
                             "has_thesis": ctx["has_thesis"], "falsified": ctx["falsified"],
                             "margin_ok": ctx["margin_ok"], **pa},
                "action": pa["action"], "action_level": pa["level"],
                "ledger": live, "backtest": bt, "bars_n": len(bars), "_chk": chk}

    out = await asyncio.gather(*(one(st) for st in all_stocks))
    method_check = _agg_check([x.pop("_chk", None) for x in out])
    SIG.record_ledger(today, [x for x in out if x.get("close") is not None])
    L2 = SIG.read_ledger()
    bars_by: dict[str, list[dict]] = {}
    async def bars_of(st: dict) -> None:
        try:
            bars_by[st["code"]] = await asyncio.to_thread(fetch_kline, kline_sym_of(st.get("gtimg", ""), st), 640)
        except Exception:
            bars_by[st["code"]] = []
    await asyncio.gather(*(bars_of(st) for st in all_stocks))
    LM = [e for e in L2["entries"] if e.get("model") == SIG.MODEL]

    def pairs(n: int) -> list[dict]:
        return [{"label": e["label"], "ret": _fwd_ret(bars_by[e["code"]], e["kline_date"], n)
                 if bars_by.get(e.get("code")) else None} for e in LM]

    def bt_agg(k: str) -> dict:
        n = hit = 0
        edge = 0.0
        for x in out:
            b = (x.get("backtest") or {}).get(k)
            if b and b.get("n"):
                n += b["n"]
                hit += b.get("hit") or 0
                edge += (b.get("edge") or 0) * b["n"]
        return {"n": n, "hit": hit, "rate": round(hit / n * 100, 1) if n else None,
                "edge": round(edge / n, 2) if n else None}

    actions: dict[str, int] = {}
    for x in out:
        actions[x["action"]] = actions.get(x["action"], 0) + 1
    data = {"ok": True, "at": bj_full(), "today": today,
            "weights": SIG.W, "labels": SIG.LABEL, "threshold": SIG.TH, "items": out,
            "summary": {
                "ledger": {"model": SIG.MODEL, "entries": len(LM), "legacy": len(L2["entries"]) - len(LM),
                           "days": len({e["date"] for e in LM}),
                           "since": min((e["date"] for e in LM), default=None),
                           "h20": _tally(pairs(20)), "h60": _tally(pairs(60))},
                "backtest": {"note": "与实盘同一套 3 个价格因子（趋势/动量/位置）逐日重算，过去约 250 个交易日",
                             "h20": bt_agg("h20"), "h60": bt_agg("h60")},
                "method_check": {"note": "独立样本（每隔 h 天取一点）、收益扣同期该股平均收益；spread = 看多后超额 − 看空后超额，> 2 倍标准误才算显著",
                                 **method_check},
                "actions": actions}}
    if not only:  # 单股刷新不覆盖全量缓存
        _cache = (time.time(), data)
    return data


async def get_signal(code: str) -> dict | None:
    d = await build_signals(False)
    return next((x for x in d.get("items", []) if x.get("code") == code), None)


def invalidate() -> None:
    global _cache
    _cache = (0.0, {})


async def refresh_signal_one(code: str) -> dict | None:
    """单股重算（不读不写全量缓存），并把结果合并进既有缓存。"""
    d = await build_signals(True, only=code)
    global _cache
    base = _cache[1]
    if base and base.get("items") and d.get("items"):
        items = [x for x in base["items"] if x.get("code") != code] + list(d["items"])
        items.sort(key=lambda x: x.get("code") or "")
        _cache = (time.time(), {**base, "items": items})
    return d

