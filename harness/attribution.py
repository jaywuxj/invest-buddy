"""交易与收益归因（2026-10-16 用户要求补齐）。

口径（四项严格加总 = 当前持仓浮动盈亏人民币；已实现/分红另列，不参与四项）：
  市场  = Σ 每笔买入 投入本金(成交日汇率折人民币) ×（基准指数涨跌幅：成交日收盘 → 最新收盘）
  汇率  = Σ 每笔买入 本金原币 ×（最新汇率 − 成交日汇率）
  时机  = Σ 股数 ×（成交日前后 10 个交易日均价 − 成交价）× 最新汇率  （买在均价之下为正贡献）
  选股  = 浮动盈亏 − 市场 − 汇率 − 时机  （残差，含价格×汇率交叉项）
基准：港股→恒生指数 hkHSI；美股→标普 500（akshare .INX）。汇率：whHKDCNY / whUSDCNY 日 K。
数据源：个股与基准、汇率日 K 全部本机自产（kline.py / akshare），不依赖 WorkBuddy。
"""
from __future__ import annotations

import time
from typing import Any

from . import kline, settings
from .store import atomic_write_json, read_json

TTL_SEC = 24 * 3600
_BENCH = {"HKD": ("hkHSI", "恒生指数"), "USD": (".INX", "标普 500")}
_FX = {"HKD": "whHKDCNY", "USD": "whUSDCNY"}


def _series(bars: list[dict]) -> dict[str, float]:
    return {b["d"]: b["c"] for b in bars if b.get("d") and b.get("c")}


def _on_or_before(s: dict[str, float], day: str) -> tuple[str, float] | None:
    ks = [k for k in s if k <= day]
    return (ks[-1], s[ks[-1]]) if ks else None


def _ma_window(bars: list[dict], day: str, n: int = 10) -> float | None:
    """成交日前后各 10 个交易日均价（21 日窗口）。"""
    i = next((i for i, b in enumerate(bars) if b["d"] >= day), len(bars))
    if i >= len(bars):
        return None
    if bars[i]["d"] > day and i > 0:
        i -= 1
    seg = [b["c"] for b in bars[max(0, i - n): i + n + 1] if b.get("c")]
    return sum(seg) / len(seg) if seg else None


def _fetch_all(codes_gtimg: dict[str, str], min_date: str) -> tuple[dict, dict, dict]:
    """一次拉全：个股日 K / 基准日 K / 汇率日 K。失败的部分如实在 meta 里列出。"""
    stocks, bench, fx = {}, {}, {}
    for code, gt in codes_gtimg.items():
        try:
            bars = kline.fetch_kline(kline.kline_sym_of(gt), 400)  # 美股自动补 .N/.OQ 后缀
            if bars:
                stocks[code] = bars
        except Exception:
            pass
    for ccy, (sym, name) in _BENCH.items():
        try:
            if sym.startswith("."):
                import akshare as ak
                df = ak.index_us_stock_sina(symbol=sym)
                bench[ccy] = [{"d": str(r["date"])[:10], "c": float(r["close"])} for _, r in df.iterrows()]
            else:
                bench[ccy] = kline.fetch_kline(sym, 400)
        except Exception:
            pass
    for ccy, sym in _FX.items():
        try:
            fx[ccy] = kline.fetch_kline(sym, 400)
        except Exception:
            pass
    return stocks, bench, fx


def compute(holdings: dict, trades_items: list[dict], by: dict) -> dict:
    """返回 {"rows", "totals", "available", "meta"}。全部基于当前持仓的买入批次。"""
    pos_codes = {p["code"] for p in holdings.get("positions") or []}
    buys = [t for t in trades_items if t.get("side") in ("init", "buy") and t.get("code") in pos_codes]
    if not buys:
        return {"rows": [], "totals": {}, "available": False, "meta": "没有买入流水"}
    ent = {s["code"]: s for s in (holdings.get("positions") or []) + (holdings.get("watch") or [])}
    min_date = min(t["date"] for t in buys)
    codes = {c: ent[c].get("gtimg") for c in pos_codes if ent.get(c) and ent[c].get("gtimg")}
    stocks, bench, fx = _fetch_all(codes, min_date)
    missing = [c for c in pos_codes if c not in stocks]
    missing += [_BENCH[c][1] for c in _BENCH if c not in bench]
    missing += [_FX[c] for c in _FX if c not in fx]

    rows = []
    tot = {"market": 0.0, "fx": 0.0, "timing": 0.0, "selection": 0.0, "pnl": 0.0}
    for code in sorted(pos_codes, key=lambda c: ent.get(c, {}).get("name") or c):
        st = ent.get(code) or {}
        ccy = st.get("currency") or "HKD"
        bars = stocks.get(code)
        if not bars or ccy == "CNY" or ccy not in bench or ccy not in fx:
            if bars and ccy == "CNY":
                pass  # A 股暂无基准映射，先跳过
            continue
        bs, fs = _series(bench[ccy]), _series(fx[ccy])
        px_now = (bars[-1]["c"], bars[-1]["d"])
        idx_now, fx_now = bs[sorted(bs)[-1]], fs[sorted(fs)[-1]]
        m = sel = timing = fx_c = pnl = 0.0
        n_batches = 0
        for t in buys:
            if t["code"] != code:
                continue
            n, p_t, d_t = t.get("shares") or 0, t.get("price"), t.get("date")
            if not n or not p_t or not d_t:
                continue
            n_batches += 1
            ib, fb = _on_or_before(bs, d_t), _on_or_before(fs, d_t)
            if not ib or not fb:
                continue
            fx_t, idx_t = fb[1], ib[1]
            invested_cny = n * p_t * fx_t
            market = invested_cny * (idx_now / idx_t - 1)
            fxv = n * p_t * (fx_now - fx_t)
            ma = _ma_window(bars, d_t)
            tim = n * (ma - p_t) * fx_now if ma is not None else 0.0
            m += market; fx_c += fxv; timing += tim
            pnl += n * (px_now[0] - p_t) * fx_now
        sel = pnl - m - fx_c - timing
        rows.append({"code": code, "name": st.get("name"), "currency": ccy,
                     "pnl_cny": round(pnl, 0), "market_cny": round(m, 0), "selection_cny": round(sel, 0),
                     "timing_cny": round(timing, 0), "fx_cny": round(fx_c, 0), "batches": n_batches})
        for k, v in (("market", m), ("fx", fx_c), ("timing", timing), ("selection", sel), ("pnl", pnl)):
            tot[k] += v
    totals = {k + "_cny": round(v, 0) for k, v in tot.items()}
    available = bool(rows) and not missing
    return {"rows": rows, "totals": totals, "available": available,
            "meta": {"missing": missing, "as_of": px_now[1] if rows else None,
                     "bench": {c: _BENCH[c][1] for c in bench},
                     "note": "归因覆盖当前持仓的买入批次；已卖出部分的收益在「已实现/分红」列，不参与四项分解"}}


def compute_cached(holdings: dict, trades_items: list[dict], by: dict, force: bool = False) -> dict:
    """按天缓存（data/panel/attribution_cache.json）；失败回退上次结果。"""
    f = settings.PANEL_OUT_DIR / "attribution_cache.json"
    hit = read_json(f, None)
    today = time.strftime("%Y-%m-%d")
    if not force and hit and hit.get("date") == today and hit.get("result"):
        return {**hit["result"], "cached": True}
    try:
        res = compute(holdings, trades_items, by)
    except Exception as exc:
        if hit and hit.get("result"):
            return {**hit["result"], "cached": True, "stale": True, "refresh_error": str(exc)[:120]}
        raise
    atomic_write_json(f, {"date": today, "result": res})
    return res
