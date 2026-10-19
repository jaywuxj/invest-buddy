"""信号准确率看板（移植 8899 accuracy.js）。

三个口径，全部给出 20 / 60 / 120 个交易日的方向命中率与相对基准超额：
  ① 价格因子回测 backtest：3 个价格因子在过去 ≤500 个交易日逐日重算（signals_engine）；
  ② 信号账本 live：data/panel/signal_ledger.json（signals_service 每日写入）；
  ③ 专家账本 experts：data/panel/expert_ledger.json，专家观点日收盘为起点按大师拆分。
超额 = 个股收益 − 基准收益（港股 恒生指数 hkHSI / 美股 QQQ / A 股 沪深300）。
D7 反向调节 persona_mult：超额命中样本 n ≥ 30 才生效，mult = clamp(1 + k×(rate−50)/25, 0.5, 1.5)。
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any

from . import portfolio, signals_engine as SIG
from .kline import bj_full, fetch_kline, kline_sym_of
from . import settings
from .store import atomic_write_json, read_json

H = (20, 60, 120)
BENCH = {"HKD": {"sym": "hkHSI", "name": "恒生指数"},
         "USD": {"sym": "usQQQ.OQ", "name": "纳指100(QQQ)"},
         "CNY": {"sym": "sh000300", "name": "沪深300"}}
MIN_N = 30
WINDOW = 500
_TTL_SEC = 1800

def _ledger_file() -> Path:
    return settings.PANEL_OUT_DIR / "expert_ledger.json"
_cache: tuple[float, dict] = (0.0, {})


def _r1(x):
    return None if x is None or not math.isfinite(x) else round(x, 1)


def _r2(x):
    return None if x is None or not math.isfinite(x) else round(x, 2)


def _clamp(x, a, b):
    return max(a, min(b, x))


def _idx_on_or_before(rows: list[dict], d: str) -> int:
    lo, hi, ans = 0, len(rows) - 1, -1
    while lo <= hi:
        m = (lo + hi) >> 1
        if rows[m]["d"] <= d:
            ans = m
            lo = m + 1
        else:
            hi = m - 1
    return ans


def _idx_on_or_after(rows: list[dict], d: str) -> int:
    lo, hi, ans = 0, len(rows) - 1, -1
    while lo <= hi:
        m = (lo + hi) >> 1
        if rows[m]["d"] >= d:
            ans = m
            hi = m - 1
        else:
            lo = m + 1
    return ans


def _bench_ret(bench: list[dict], d0: str, d1: str) -> float | None:
    if not bench:
        return None
    i0, i1 = _idx_on_or_before(bench, d0), _idx_on_or_before(bench, d1)
    if i0 < 0 or i1 < 0 or i1 <= i0:
        return None
    return (bench[i1]["c"] / bench[i0]["c"] - 1) * 100


def _acc() -> dict:
    return {"n": 0, "hit": 0, "sum": 0.0, "xn": 0, "xhit": 0, "xsum": 0.0}


def _add(a: dict, label: str, ret: float | None, ex: float | None) -> None:
    if ret is None or label == "中性":
        return
    sg = 1 if label == "看多" else -1
    a["n"] += 1
    if sg * ret > 0:
        a["hit"] += 1
    a["sum"] += sg * ret
    if ex is not None:
        a["xn"] += 1
        if sg * ex > 0:
            a["xhit"] += 1
        a["xsum"] += sg * ex


def _merge(a: dict, b: dict) -> dict:
    for k in a:
        a[k] += b[k]
    return a


def _fin(a: dict) -> dict:
    return {"n": a["n"], "hit": a["hit"], "rate": _r1(a["hit"] / a["n"] * 100) if a["n"] else None,
            "edge": _r2(a["sum"] / a["n"]) if a["n"] else None,
            "ex_n": a["xn"], "ex_rate": _r1(a["xhit"] / a["xn"] * 100) if a["xn"] else None,
            "ex_edge": _r2(a["xsum"] / a["xn"]) if a["xn"] else None}


def _base_acc() -> dict:
    return {"n": 0, "up": 0, "sum": 0.0, "xn": 0, "xup": 0, "xsum": 0.0}


def _fin_base(b: dict) -> dict:
    return {"n": b["n"], "up_rate": _r1(b["up"] / b["n"] * 100) if b["n"] else None,
            "avg": _r2(b["sum"] / b["n"]) if b["n"] else None,
            "ex_up_rate": _r1(b["xup"] / b["xn"] * 100) if b["xn"] else None,
            "ex_avg": _r2(b["xsum"] / b["xn"]) if b["xn"] else None}


def _label_of(s: float) -> str:
    return "看多" if s >= SIG.TH else "看空" if s <= -SIG.TH else "中性"


_PF = ("trend", "momentum", "position")


def _backtest_stock(rows: list[dict], bench: list[dict]) -> dict:
    closes = [x["c"] for x in rows]
    res = {"combo": {}, "factors": {}, "base": {}, "monthly": {}, "samples": 0, "from": None, "to": None}
    for h in H:
        res["combo"][h] = _acc()
        res["base"][h] = _base_acc()
        for k in _PF:
            res["factors"].setdefault(k, {})[h] = _acc()
    start = max(120, len(closes) - WINDOW)
    for t in range(start, len(closes) - H[0]):
        f = SIG.price_factors(closes, t)
        ws = ss = 0.0
        for k in _PF:
            if k in f:
                ws += SIG.W[k]
                ss += SIG.W[k] * f[k]["s"]
        if not ws:
            continue
        lab = _label_of(ss / ws)
        res["samples"] += 1
        if not res["from"]:
            res["from"] = rows[t]["d"]
        res["to"] = rows[t]["d"]
        for h in H:
            if t + h >= len(closes):
                continue
            ret = (closes[t + h] / closes[t] - 1) * 100
            bx = _bench_ret(bench, rows[t]["d"], rows[t + h]["d"])
            ex = ret - bx if bx is not None else None
            _add(res["combo"][h], lab, ret, ex)
            for k in _PF:
                if k in f:
                    _add(res["factors"][k][h], _label_of(f[k]["s"]), ret, ex)
            b = res["base"][h]
            b["n"] += 1
            if ret > 0:
                b["up"] += 1
            b["sum"] += ret
            if ex is not None:
                b["xn"] += 1
                if ex > 0:
                    b["xup"] += 1
                b["xsum"] += ex
            if h == 20:
                mk = rows[t]["d"][:7]
                _add(res["monthly"].setdefault(mk, _acc()), lab, ret, ex)
    return res


def _fwd(rows: list[dict], bench: list[dict], d: str, h: int) -> dict | None:
    i = _idx_on_or_after(rows, d)
    if i < 0 or i + h >= len(rows):
        return None
    ret = (rows[i + h]["c"] / rows[i]["c"] - 1) * 100
    bx = _bench_ret(bench, rows[i]["d"], rows[i + h]["d"])
    return {"ret": ret, "ex": ret - bx if bx is not None else None,
            "from": rows[i]["d"], "to": rows[i + h]["d"]}


def _mature_eta(rows: list[dict], d: str, h: int) -> int:
    i = _idx_on_or_after(rows, d)
    done = 0 if i < 0 else len(rows) - 1 - i
    return max(0, h - done)


def _read_expert_ledger() -> dict:
    d = read_json(_ledger_file(), None)
    return d if isinstance(d, dict) and isinstance(d.get("entries"), list) else {"updated_at": None, "entries": []}


def snapshot_experts() -> dict:
    """专家账本快照：每个（标的, 大师, generated_at）只记一条。"""
    ex = read_json(settings.PANEL_OUT_DIR / "experts.json", None) or read_json(
        settings.DATA_DIR / "experts.json", {}) or {}
    stocks = ex.get("stocks") or {}
    L = _read_expert_ledger()
    seen = {f"{e.get('code')}|{e.get('persona')}|{e.get('gen_at')}" for e in L["entries"]}
    stock_p = {p.get("id") for p in portfolio.load_personas() if (p.get("scope") or "stock") == "stock"}
    added = 0
    for code, per in stocks.items():
        for pid, r in (per or {}).items():
            if not r or pid not in stock_p or r.get("score") is None or not r.get("generated_at"):
                continue
            k = f"{code}|{pid}|{r['generated_at']}"
            if k in seen:
                continue
            seen.add(k)
            added += 1
            dt = _bj_date(r["generated_at"])
            L["entries"].append({"date": dt, "code": code, "persona": pid, "score": r["score"],
                                 "stance": r.get("stance") or ("看多" if r["score"] > 0 else "看空" if r["score"] < 0 else "中性"),
                                 "gen_at": r["generated_at"]})
    if added or not L.get("updated_at"):
        L["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(_ledger_file(), L)
    return {"added": added, "total": len(L["entries"])}


def _bj_date(iso: str) -> str | None:
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
        return datetime.fromtimestamp(ts + 8 * 3600, timezone.utc).date().isoformat()
    except (ValueError, TypeError):
        return None


async def view(force: bool = False) -> dict:
    global _cache
    if not force and _cache[1] and time.time() - _cache[0] < _TTL_SEC:
        return _cache[1]
    snap = snapshot_experts()
    h = portfolio.load_holdings()
    all_stocks = [{**x, "held": True} for x in (h.get("positions") or [])] + [
        {**x, "held": False} for x in (h.get("watch") or [])]
    bench: dict[str, list[dict]] = {}
    async def load_bench(cur: str, sym: str) -> None:
        try:
            bench[cur] = await asyncio.to_thread(fetch_kline, sym, 640)
        except Exception:
            bench[cur] = []
    await asyncio.gather(*(load_bench(c, b["sym"]) for c, b in BENCH.items()))
    bars_by: dict[str, list[dict]] = {}
    async def load_bars(st: dict) -> None:
        try:
            bars_by[st["code"]] = await asyncio.to_thread(fetch_kline, kline_sym_of(st.get("gtimg", ""), st), 640)
        except Exception:
            bars_by[st["code"]] = []
    await asyncio.gather(*(load_bars(st) for st in all_stocks))

    # ① 回测
    tot = {"combo": {}, "factors": {}, "base": {}, "monthly": {}}
    for hh in H:
        tot["combo"][hh] = _acc()
        tot["base"][hh] = _base_acc()
        for k in _PF:
            tot["factors"].setdefault(k, {})[hh] = _acc()
    by_mkt: dict[str, dict] = {}
    stocks = []
    for st in all_stocks:
        rows = bars_by.get(st["code"]) or []
        cur = st.get("currency") or "HKD"
        if len(rows) < 200:
            stocks.append({"code": st["code"], "name": st.get("name"), "held": st["held"],
                           "currency": cur, "bars_n": len(rows), "error": "日 K 不足 200 根"})
            continue
        b = _backtest_stock(rows, bench.get(cur) or [])
        for hh in H:
            _merge(tot["combo"][hh], b["combo"][hh])
            _merge(tot["base"][hh], b["base"][hh])
            for k in _PF:
                _merge(tot["factors"][k][hh], b["factors"][k][hh])
            _merge(by_mkt.setdefault(cur, {}).setdefault(hh, _acc()), b["combo"][hh])
        for mk, v in b["monthly"].items():
            _merge(tot["monthly"].setdefault(mk, _acc()), v)
        o = {"code": st["code"], "name": st.get("name"), "held": st["held"], "currency": cur,
             "bars_n": len(rows), "samples": b["samples"], "from": b["from"], "to": b["to"]}
        for hh in H:
            o[f"h{hh}"] = _fin(b["combo"][hh])
            o[f"base{hh}"] = _fin_base(b["base"][hh])
        stocks.append(o)

    def fmt_h(m: dict) -> dict:
        return {f"h{hh}": _fin(m.get(hh) or _acc()) for hh in H}

    backtest = {"note": f"仅价格因子（趋势/动量/位置）逐日重算，最近 ≤{WINDOW} 个交易日；逐日样本持有期重叠，"
                        "有效独立样本约 n/h，勿把命中率当作精确概率",
                "combo": fmt_h(tot["combo"]),
                "base": {f"h{hh}": _fin_base(tot["base"][hh]) for hh in H},
                "factors": [{"key": k, "name": SIG.LABEL[k], **fmt_h(tot["factors"][k])} for k in _PF],
                "markets": [{"currency": c, "bench": BENCH[c]["name"], **fmt_h(m)}
                            for c, m in by_mkt.items()],
                "monthly": [{"month": mk, **_fin(v)} for mk, v in sorted(tot["monthly"].items())],
                "stocks": stocks}

    # ② 信号账本
    SL = SIG.read_ledger()
    cur_of = {st["code"]: st.get("currency") or "HKD" for st in all_stocks}
    live_tot: dict[int, dict] = {}
    live_by_conf: dict[str, dict] = {}
    live_pending = 0
    LM = [e for e in SL["entries"] if e.get("model") == SIG.MODEL]
    for e in LM:
        rows = bars_by.get(e.get("code"))
        if not rows or not e.get("kline_date"):
            continue
        any_hit = False
        for hh in H:
            f = _fwd(rows, bench.get(cur_of.get(e["code"], "HKD")) or [], e["kline_date"], hh)
            if not f:
                continue
            any_hit = True
            _add(live_tot.setdefault(hh, _acc()), e.get("label"), f["ret"], f["ex"])
            lv = "?" if e.get("conf") is None else "高" if e["conf"] >= 0.6 else "中" if e["conf"] >= 0.35 else "低"
            _add(live_by_conf.setdefault(lv, {}).setdefault(hh, _acc()), e.get("label"), f["ret"], f["ex"])
        if not any_hit and e.get("label") != "中性":
            live_pending += 1
    live_dates = sorted(e.get("date") for e in LM if e.get("date"))
    e0 = next((e for e in LM if e.get("kline_date") and bars_by.get(e.get("code"))), None)
    live = {"entries": len(LM), "directional": sum(1 for e in LM if e.get("label") != "中性"),
            "pending": live_pending, "since": live_dates[0] if live_dates else None,
            "days": len(set(live_dates)),
            "first_mature_in": _mature_eta(bars_by[e0["code"]], e0["kline_date"], 20) if e0 else None,
            "tot": fmt_h(live_tot),
            "by_conf": [{"level": lv, **fmt_h(live_by_conf.get(lv) or {})} for lv in ("高", "中", "低")]}

    # ③ 专家账本
    EL = _read_expert_ledger()
    personas = [p for p in portfolio.load_personas() if (p.get("scope") or "stock") == "stock"]
    p_acc: dict[str, dict] = {}
    p_info: dict[str, dict] = {}
    for p in personas:
        p_acc[p["id"]] = {}
        p_info[p["id"]] = {"opinions": 0, "directional": 0, "pending": 0, "eta": None, "bull": 0, "bear": 0}
    for e in EL["entries"]:
        if e.get("persona") not in p_acc:
            continue
        I = p_info[e["persona"]]
        I["opinions"] += 1
        lab = "看多" if (e.get("score") or 0) > 0 else "看空" if (e.get("score") or 0) < 0 else "中性"
        if lab != "中性":
            I["directional"] += 1
            if lab == "看多":
                I["bull"] += 1
            else:
                I["bear"] += 1
        rows = bars_by.get(e.get("code"))
        if not rows or not e.get("date"):
            continue
        any_hit = False
        for hh in H:
            f = _fwd(rows, bench.get(cur_of.get(e["code"], "HKD")) or [], e["date"], hh)
            if not f:
                continue
            any_hit = True
            _add(p_acc[e["persona"]].setdefault(hh, _acc()), lab, f["ret"], f["ex"])
        if not any_hit and lab != "中性":
            I["pending"] += 1
            eta = _mature_eta(rows, e["date"], 20)
            I["eta"] = eta if I["eta"] is None else min(I["eta"], eta)
    persona_mult: dict[str, float] = {}
    experts = []
    for p in personas:
        st = fmt_h(p_acc[p["id"]])
        basis = n = rate = None
        if (st["h60"].get("ex_n") or 0) >= MIN_N:
            basis, n, rate = "h60", st["h60"]["ex_n"], st["h60"]["ex_rate"]
        elif (st["h20"].get("ex_n") or 0) >= MIN_N:
            basis, n, rate = "h20", st["h20"]["ex_n"], st["h20"]["ex_rate"]
        mult = 1.0
        if basis:
            k = n / (n + 30)
            mult = _r2(_clamp(1 + (k * (rate - 50)) / 25, 0.5, 1.5))
        persona_mult[p["id"]] = mult
        experts.append({"id": p["id"], "name": p.get("name"), "mult": mult, "basis": basis,
                        "need": 0 if basis else max(0, MIN_N - max(st["h20"].get("ex_n") or 0, st["h60"].get("ex_n") or 0)),
                        **p_info[p["id"]], **st})
    data = {"ok": True, "at": bj_full(), "horizons": list(H), "min_n": MIN_N, "bench": BENCH,
            "rule": f"某位大师超额命中样本 n≥{MIN_N} 才调节 D7 权重：优先 60 日口径，不足退用 20 日；"
                    "mult = clamp(1 + n/(n+30) × (超额命中率−50)/25, 0.5, 1.5)，与能力圈权重相乘",
            "backtest": backtest, "live": live, "experts": experts, "persona_mult": persona_mult,
            "expert_ledger": {"entries": len(EL["entries"]), "added_now": snap["added"],
                              "since": min((e["date"] for e in EL["entries"] if e.get("date")), default=None)}}
    _cache = (time.time(), data)
    return data


def mult() -> dict:
    return (_cache[1] or {}).get("persona_mult") or {}


def invalidate() -> None:
    global _cache
    _cache = (0.0, {})
