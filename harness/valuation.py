"""动态估值引擎（逐行移植 8899 valuation.js，数据源 = 本机 westock CLI）。

三把尺子：
  1) PE / PB / PS 5 年分位带（周频，260 周）
  2) 可比公司横比（data/config/peers.json，westock quote 批量）
  3) 反向 DCF：市值隐含的 10 年 FCF 增速 + 基准情景内在价值与安全边际（≥30% 着色）

口径要点（避免币种坑）：
  - 财报披露币种与报价币种可能不同（港股财报常为人民币，TSM 标 TWD 实为 ADR 口径）。
    不直接用「价格 / 每股收益」，而是以当前 PB（quote.pb_ratio）为锚：
      市值_财报币种(now) = pb_now × 归母净资产(now)
      市值_财报币种(t)   = 市值(now) × price_t / price_now
      PE_t = 市值(t) / 归母净利TTM(t)；PB_t、PS_t 同理 —— 全部比值都在财报币种内完成。
  - 港股 finance 为累计口径（PeriodMark=累计月数），TTM = 本期累计 + 上一完整财年 − 上年同期累计；
    美股为单季口径（*_Q），TTM = 最近 4 个单季之和。
  - 历史点只使用「已披露」财报：港股按 InfoPublDate，美股按 EndDate+45 天，避免未来函数。
  - 反向 DCF 使用股权自由现金流近似（FCF = 经营现金流 − 资本开支），未加回净现金（保守口径）。
"""
from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import portfolio, settings
from .providers import westock
from .store import atomic_write_json, read_json

YEAR_SEC = 365.25 * 86400
WACC = {"hk": float(os.environ.get("VAL_WACC_HK", "0.09")),
        "us": float(os.environ.get("VAL_WACC_US", "0.085")),
        "cn": float(os.environ.get("VAL_WACC_CN", "0.08"))}
TERM_G = float(os.environ.get("VAL_TERM_G", "0.03"))
MOS_OK = 0.30
HIGH_GROWTH = 0.5  # 营收同比 > 50% 视为高成长：历史分位降权
G_CAP = float(os.environ.get("VAL_G_CAP", "0.15"))
PCTS = (10, 25, 50, 75, 90)
TTL_SEC = 24 * 3600

_UTC = timezone.utc


def _ws_code(gtimg_or_code: str) -> str | None:
    s = str(gtimg_or_code or "")
    return s if s[:2].lower() in ("hk", "us", "sh", "sz") else None


def _mkt(ws: str) -> str:
    return "us" if ws[:2].lower() == "us" else "hk" if ws[:2].lower() == "hk" else "cn"


def _tms(s: str) -> float:
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").replace(tzinfo=_UTC).timestamp()


def _ymd(ts: float) -> str:
    return datetime.fromtimestamp(ts, _UTC).date().isoformat()


def _months_back(date_str: str, m: int) -> str:
    import calendar
    d = datetime.fromtimestamp(_tms(date_str), _UTC)
    y, mo = d.year, d.month - m
    while mo < 1:
        mo += 12
        y -= 1
    return datetime(y, mo, calendar.monthrange(y, mo)[1], tzinfo=_UTC).date().isoformat()


def quantile(sorted_arr: list[float], p: float) -> float | None:
    if not sorted_arr:
        return None
    i = (len(sorted_arr) - 1) * p / 100
    lo, hi = math.floor(i), math.ceil(i)
    return sorted_arr[lo] + (sorted_arr[hi] - sorted_arr[lo]) * (i - lo)


def rank_pct(sorted_arr: list[float], v: float | None) -> int | None:
    if not sorted_arr or v is None:
        return None
    k = sum(1 for x in sorted_arr if x <= v)
    return round(k / len(sorted_arr) * 100)


# ---------- 财报 → 季度基本面序列 [{end, pub, ni, rev, eq, ocf, capex}]（TTM 口径，财报币种） ----------
def hk_series(inc: list[dict], bal: list[dict], cf: list[dict]) -> list[dict]:
    def by_end(rows):
        return {r["EndDate"][:10]: r for r in rows if r.get("EndDate")}

    I, B, C = by_end(inc), by_end(bal), by_end(cf)

    def ttm(M, end, f):
        r = M.get(end)
        if not r:
            return None
        v = westock.num(r.get(f))
        if v is None:
            return None
        pm = westock.num(r.get("PeriodMark")) or (int(end[5:7]) % 12 or 12)
        if pm >= 12:
            return v
        fy = M.get(_months_back(end, int(pm)))
        prev = M.get(_months_back(end, 12))
        if not fy or not prev or (westock.num(fy.get("PeriodMark")) or 12) < 12:
            return None
        a, b = westock.num(fy.get(f)), westock.num(prev.get(f))
        return None if a is None or b is None else v + a - b

    out = []
    for end in sorted(I):
        r = I[end]
        pub_raw = r.get("InfoPublDate") or ""
        pub = pub_raw[:10] if pub_raw[:4].isdigit() else _ymd(_tms(end) + 75 * 86400)
        out.append({"end": end, "pub": pub,
                    "ni": ttm(I, end, "ProfitToShareholders"), "rev": ttm(I, end, "OperatingIncome"),
                    "eq": westock.num(B[end].get("SeWithoutMinority")) if end in B else None,
                    "ocf": ttm(C, end, "CFO"), "capex": ttm(C, end, "Purcapitalassents"),
                    "cur": r.get("CurrencyType") or ""})
    return out


def us_series(inc: list[dict], bal: list[dict], cf: list[dict]) -> list[dict]:
    I = sorted((r for r in inc if r.get("EndDate")), key=lambda r: r["EndDate"])
    Bm = {r["EndDate"][:10]: r for r in bal if r.get("EndDate")}
    Cm = {r["EndDate"][:10]: r for r in cf if r.get("EndDate")}

    def sum4(arr, i, f, M=None):
        if i < 3:
            return None
        span = _tms(arr[i]["EndDate"]) - _tms(arr[i - 3]["EndDate"])
        if span > 300 * 86400 or span < 240 * 86400:  # 必须是连续 4 个季度
            return None
        s = 0.0
        for k in range(i - 3, i + 1):
            row = (M or {}).get(arr[k]["EndDate"][:10]) if M else arr[k]
            v = westock.num(row.get(f)) if row else None
            if v is None:
                return None
            s += v
        return s

    out = []
    for i, r in enumerate(I):
        end = r["EndDate"][:10]
        b = Bm.get(end)
        eq = None
        if b:
            eq = westock.num(b.get("ShareHolderEquity"))
            if eq is None:
                eq = westock.num(b.get("TotalEquity"))
        out.append({"end": end, "pub": _ymd(_tms(end) + 45 * 86400),
                    "ni": sum4(I, i, "NetIncome_Q"), "rev": sum4(I, i, "Sales_Q"),
                    "eq": eq, "ocf": sum4(I, i, "CFO_Q", Cm), "capex": sum4(I, i, "Capex_Q", Cm),
                    "cur": r.get("DisclosureCurrency") or ""})
    return out


# ---------- 反向 DCF ----------
def dcf_value(fcf0: float, g1: float, r: float, gt: float, fade_to: float | None = None) -> float:
    """1-5 年增速 g1，6-10 年线性衰减到 fade_to（缺省即 g1 恒定），终值永续 gt。"""
    v, f = 0.0, fcf0
    for t in range(1, 11):
        g = g1 if fade_to is None or t <= 5 else g1 + (fade_to - g1) * (t - 5) / 5
        f *= 1 + g
        v += f / (1 + r) ** t
    return v + f * (1 + gt) / (r - gt) / (1 + r) ** 10


def implied_growth(fcf0: float, target: float, r: float, gt: float) -> dict:
    lo, hi = -0.3, 0.8
    if dcf_value(fcf0, lo, r, gt) > target:
        return {"g": lo, "bound": "below"}
    if dcf_value(fcf0, hi, r, gt) < target:
        return {"g": hi, "bound": "above"}
    for _ in range(80):
        m = (lo + hi) / 2
        if dcf_value(fcf0, m, r, gt) > target:
            hi = m
        else:
            lo = m
    return {"g": (lo + hi) / 2, "bound": None}


def _cache_file():
    return settings.PANEL_OUT_DIR / "valuation_cache.json"


def _peers_file():
    return settings.CONFIG_DIR / "peers.json"


def _bj_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()[:19]


def _heavy(ws: str, force: bool) -> dict:
    """重数据（财报 + 周 K）按 24h 落盘缓存；失败时有旧缓存回旧缓存。"""
    c = read_json(_cache_file(), {}) or {}
    hit = c.get(ws)
    if not force and hit and time.time() - hit.get("at_epoch", 0) < TTL_SEC and hit.get("series"):
        return {**hit, "cached": True}
    try:
        us = _mkt(ws) == "us"
        inc = westock.finance(ws, "income", 28 if us else 30)
        bal = westock.finance(ws, "balance", 28 if us else 30)
        cf = westock.finance(ws, "cashflow", 28 if us else 30)
        bars = westock.kline(ws, "week", 262, "qfq")
        series = us_series(inc, bal, cf) if us else hk_series(inc, bal, cf)
        if not series or not bars:
            raise RuntimeError("财报或周 K 为空")
        payload = {"ws": ws, "series": series, "bars": bars, "at": _bj_iso(), "at_epoch": time.time()}
        c[ws] = payload
        atomic_write_json(_cache_file(), c)
        return payload
    except Exception:
        if hit and hit.get("series"):
            return {**hit, "cached": True, "stale": True}
        raise


def read_peers() -> dict:
    return (read_json(_peers_file(), {}) or {}).get("items") or {}


def judge(rulers: dict, primary: str) -> tuple:
    """三把尺子 → (可用数, 总分, 可信度, 结论, 旧版结论词)。
    可信度：只有 0-1 把尺子 = 低（结论最多到「偏」）；2 把或主口径只能看市销率 = 中；3 把 = 高。"""
    vals = [r_["s"] for r_ in rulers.values() if r_.get("s") is not None]
    n_ok = len(vals)
    if not n_ok:
        return 0, None, "低", "暂无结论", "暂无结论"
    score = sum(vals)
    confidence = "低" if n_ok <= 1 else "中" if n_ok == 2 or primary == "ps" else "高"
    if confidence == "低":
        score = max(-3, min(3, score))
    verdict = ("明显便宜" if score >= 4 else "偏便宜" if score >= 2
               else "明显偏贵" if score <= -4 else "偏贵" if score <= -2 else "合理")
    overall = {"明显便宜": "显著低估", "偏便宜": "偏低估", "明显偏贵": "显著高估", "偏贵": "偏高估", "合理": "合理区间"}[verdict]
    return n_ok, score, confidence, verdict, overall


def view(code_or_ws: str, force: bool = False) -> dict:
    st = None
    for x in portfolio.all_stocks():
        if x.get("code") == code_or_ws or x.get("gtimg") == code_or_ws:
            st = x
            break
    ws = _ws_code(st["gtimg"] if st else code_or_ws)
    if not ws:
        return {"ok": False, "error": "not_found"}
    if not westock.available():
        return {"ok": False, "error": "westock_unavailable",
                "reason": "未安装 westock CLI，无法计算历史估值分位与反向 DCF"}
    m = _mkt(ws)
    H = _heavy(ws, force)
    peer_list = [x for x in (read_peers().get(st["code"] if st else ws) or []) if x and x != ws]
    try:
        Q = westock.quote([ws] + peer_list, force)
    except Exception:
        Q = {}
    q = Q.get(ws) or {}
    bars = H.get("bars") or []
    price = westock.num(q.get("price")) or (bars[-1]["c"] if bars else None)
    pb_now = westock.num(q.get("pb_ratio"))
    pe_q = westock.num(q.get("pe_ratio"))
    S = [x for x in (H.get("series") or []) if (x.get("eq") or 0) > 0]
    last = S[-1] if S else None
    warn: list[str] = []
    if not last or not (pb_now and pb_now > 0) or not (price and price > 0):
        return {"ok": False, "error": "insufficient", "code": st["code"] if st else ws, "ws": ws,
                "reason": "缺少财报" if not last else ("缺少 PB 锚（净资产为负或行情缺失）" if not (pb_now and pb_now > 0) else "缺少价格")}
    cap_now = pb_now * last["eq"]  # 财报币种市值
    px_now = price

    # 周频倍数序列：每周取「已披露」的最近一期
    bars5 = [b for b in bars if _tms(b["d"]) >= time.time() - 5 * YEAR_SEC - 14 * 86400]
    pts = []
    j = -1
    sorted_pub = sorted(S, key=lambda x: x["pub"])
    for b in bars5:
        while j + 1 < len(sorted_pub) and sorted_pub[j + 1]["pub"] <= b["d"]:
            j += 1
        if j < 0:
            continue
        f = sorted_pub[j]
        cap = cap_now * b["c"] / px_now
        pts.append({"d": b["d"], "px": b["c"],
                    "pe": cap / f["ni"] if (f.get("ni") or 0) > 0 else None,
                    "pb": cap / f["eq"] if (f.get("eq") or 0) > 0 else None,
                    "ps": cap / f["rev"] if (f.get("rev") or 0) > 0 else None,
                    "eps": f["ni"] * px_now / cap_now if (f.get("ni") or 0) > 0 else None,
                    "bps": f["eq"] * px_now / cap_now,
                    "sps": f["rev"] * px_now / cap_now if (f.get("rev") or 0) > 0 else None})
    cur_m = {"pe": cap_now / last["ni"] if (last.get("ni") or 0) > 0 else None,
             "pb": pb_now,
             "ps": cap_now / last["rev"] if (last.get("rev") or 0) > 0 else None}
    if cur_m["pe"] and pe_q and pe_q > 0 and abs(cur_m["pe"] / pe_q - 1) > 0.25:
        warn.append(f"自算 PE {cur_m['pe']:.1f} 与行情 PE {pe_q:.1f} 偏差 >25%（常见于一次性损益或股本变动），以分位相对位置为准")
    metrics: dict[str, dict] = {}
    for k in ("pe", "pb", "ps"):
        arr = sorted(p[k] for p in pts if p[k] is not None and 0 < p[k] < (500 if k == "pe" else 200))
        cov = len(arr) / len(pts) if pts else 0
        band = {f"p{p}": quantile(arr, p) for p in PCTS}
        cur = cur_m[k]
        pr = rank_pct(arr, cur)
        metrics[k] = {"n": len(arr), "coverage": round(cov * 100), "cur": cur, "rank": pr,
                      "band": band, "min": arr[0] if arr else None, "max": arr[-1] if arr else None,
                      "zone": "—" if pr is None else ("低估区" if pr <= 20 else "偏低" if pr <= 40 else "中枢" if pr < 60 else "偏高" if pr < 80 else "高估区"),
                      "usable": len(arr) >= 26 and cur is not None and cur > 0}
    hist_ok = metrics["pe"]["usable"] or metrics["ps"]["usable"] or metrics["pb"]["usable"]
    primary = "pe" if metrics["pe"]["usable"] and metrics["pe"]["coverage"] >= 70 else ("ps" if metrics["ps"]["usable"] else "pb")
    if not hist_ok:
        warn.append("历史倍数样本不足 26 周（新股或财报缺失），分位仅供参考、不计入结论")
    if primary != "pe":
        warn.append("近 5 年存在亏损期或 PE 覆盖不足，主口径切换为 PS" if primary == "ps" else "PE/PS 均不可用，主口径为 PB")
    years = (_tms(pts[-1]["d"]) - _tms(pts[0]["d"])) / YEAR_SEC if pts else 0
    if years < 4.5:
        warn.append(f"可用历史仅 {years:.1f} 年，分位参考性下降")

    # 分位价格带（主口径）：当前每股基本面 × 历史分位倍数
    fps_key = {"pe": "eps", "pb": "bps", "ps": "sps"}[primary]
    fps_now = {"pe": last["ni"] * px_now / cap_now if (last.get("ni") or 0) > 0 else None,
               "pb": last["eq"] * px_now / cap_now,
               "ps": last["rev"] * px_now / cap_now if (last.get("rev") or 0) > 0 else None}[primary]
    price_band = {}
    for p in PCTS:
        mb = metrics[primary]["band"][f"p{p}"]
        price_band[f"p{p}"] = mb * fps_now if mb and fps_now and fps_now > 0 else None

    # 反向 DCF
    r = WACC[m]
    fcf_row = next((x for x in reversed(S) if x.get("ocf") is not None and x.get("capex") is not None), None)
    fcf0 = (fcf_row["ocf"] - abs(fcf_row["capex"])) if fcf_row else None
    dcf: dict[str, Any] = {"ok": False, "wacc": r, "term_g": TERM_G}

    def rev_at(yrs: float):
        t = _tms(last["end"]) - yrs * YEAR_SEC
        c = [x for x in S if (x.get("rev") or 0) > 0 and abs(_tms(x["end"]) - t) < 50 * 86400]
        return {"y": yrs, "v": c[-1]["rev"]} if c else None

    base_rev = rev_at(3) or rev_at(2) or rev_at(1)
    cagr = (last["rev"] / base_rev["v"]) ** (1 / base_rev["y"]) - 1 if base_rev and (last.get("rev") or 0) > 0 else None
    if fcf0 is not None and fcf0 > 0:
        ig = implied_growth(fcf0, cap_now, r, TERM_G)
        g1 = max(-0.05, min(G_CAP, 0.05 if cagr is None else cagr))
        scen = []
        for name, g in (("保守", g1 - 0.05), ("基准", g1), ("乐观", g1 + 0.05)):
            v = dcf_value(fcf0, g, r, TERM_G, TERM_G)
            iv = v / cap_now * px_now
            scen.append({"name": name, "g1": g, "iv": iv, "mos": 1 - px_now / iv})
        sens = [{"wacc": r + dr, "iv": dcf_value(fcf0, g1, r + dr, TERM_G, TERM_G) / cap_now * px_now}
                for dr in (-0.01, 0.0, 0.01)]
        base = scen[1]
        dcf = {"ok": True, "wacc": r, "term_g": TERM_G, "fcf_ttm": fcf0, "fcf_end": fcf_row["end"],
               "fcf_yield": fcf0 / cap_now, "implied_g": ig["g"], "implied_bound": ig.get("bound"),
               "rev_cagr": cagr, "rev_cagr_years": base_rev["y"] if base_rev else None,
               "base_g1": g1, "scenarios": scen, "sens": sens, "iv": base["iv"], "mos": base["mos"],
               "mos_ok": base["mos"] >= MOS_OK,
               "verdict": "安全边际充足（≥30%）" if base["mos"] >= MOS_OK else ("有折价但不足 30%" if base["mos"] >= 0 else "高于基准内在价值"),
               "gap": None if cagr is None else ig["g"] - cagr}
    else:
        dcf["reason"] = "缺少经营现金流/资本开支数据" if fcf0 is None else "TTM 自由现金流为负，反向 DCF 不适用（看 PS 分位与亏损收窄节奏）"

    # 可比公司
    p_rows = []
    for c in [ws] + peer_list:
        x = Q.get(c)
        if not x:
            p_rows.append({"ws": c, "name": c, "missing": True})
            continue
        p_rows.append({"ws": c, "name": x.get("name") or c, "self": c == ws,
                       "pe": westock.num(x.get("pe_ratio")), "pb": westock.num(x.get("pb_ratio")),
                       "dy": westock.num(x.get("dividend_ratio_ttm")), "cap": westock.num(x.get("total_market_cap")),
                       "ytd": westock.num(x.get("chg_ytd")), "mkt": x.get("market_name") or ""})

    def med(k):
        a = sorted(x[k] for x in p_rows if not x.get("self") and not x.get("missing") and (x.get(k) or 0) > 0)
        return quantile(a, 50) if a else None

    peer_med = {"pe": med("pe"), "pb": med("pb")}
    self_q = p_rows[0] if p_rows else {}
    peers = {"list": p_rows, "median": peer_med, "configured": len(peer_list),
             "prem_pe": self_q.get("pe") / peer_med["pe"] - 1 if peer_med["pe"] and (self_q.get("pe") or 0) > 0 else None,
             "prem_pb": self_q.get("pb") / peer_med["pb"] - 1 if peer_med["pb"] and (self_q.get("pb") or 0) > 0 else None}

    # 综合结论（规则化，便于复核）
    pr = metrics[primary]["rank"] if hist_ok else None
    tags = []
    if pr is not None:
        tags.append(f"{primary.upper()} 5 年分位 {pr}%（{metrics[primary]['zone']}）")
    if dcf.get("ok"):
        tags.append(f"市值隐含 10 年 FCF 增速 {dcf['implied_g'] * 100:.1f}%" +
                    (f"，过去 {dcf['rev_cagr_years']} 年营收 CAGR {dcf['rev_cagr'] * 100:.1f}%" if dcf.get("rev_cagr") is not None else ""))
        tags.append(f"基准安全边际 {dcf['mos'] * 100:.0f}%")
    if peers["prem_pe"] is not None:
        tags.append(f"PE 较同业中位 {'溢价 ' if peers['prem_pe'] >= 0 else '折价 '}{abs(peers['prem_pe'] * 100):.0f}%")
    # 三把尺子（v2 2026-10-15）：分位 ±2 · DCF ±2 · 同业 ±1；可信度与封顶规则
    rev_1y = rev_at(1)
    rev_yoy = (last["rev"] / rev_1y["v"] - 1) if rev_1y and rev_1y["v"] > 0 and (last.get("rev") or 0) > 0 else None
    high_growth = rev_yoy is not None and rev_yoy > HIGH_GROWTH
    sP = None if pr is None else (2 if pr <= 20 else 1 if pr <= 40 else (-2 if pr >= 80 else -1 if pr >= 60 else 0))
    if sP is not None and high_growth:
        sP = max(-1, min(1, sP))  # 营收高增长会被动压低历史倍数，分位权重减半（±1）
        warn.append(f"近一年营收增长 {rev_yoy * 100:.0f}%（高成长），历史分位会被动偏低，已降权")
    sD = None if not dcf.get("ok") else (2 if dcf["mos"] >= MOS_OK else 1 if dcf["mos"] >= 0 else (-2 if dcf["mos"] < -0.3 else -1))
    sC = None if peers["prem_pe"] is None else (1 if peers["prem_pe"] <= -0.2 else (-1 if peers["prem_pe"] >= 0.3 else 0))
    peer_n = len([x for x in p_rows if not x.get("self") and not x.get("missing") and (x.get("pe") or 0) > 0])
    rulers = {
        "hist": {"s": sP, "max": 2, "na": None if sP is not None else ("历史倍数样本不足" if not hist_ok else "当前倍数不可用")},
        "dcf": {"s": sD, "max": 2, "na": None if sD is not None else (
            "自由现金流为负，没法用现金流估值" if fcf0 is not None and fcf0 <= 0 else "缺少现金流数据")},
        "peer": {"s": sC, "max": 1, "na": None if sC is not None else (
            "还没有配置可比公司" if not peer_list else "本股亏损，没法比市盈率" if not (self_q.get("pe") or 0) > 0 else "同行数据不足")},
    }
    n_ok, score, confidence, verdict, overall = judge(rulers, primary)

    return {"ok": True, "code": st["code"] if st else ws, "ws": ws,
            "name": (st or {}).get("name") or q.get("name") or ws, "market": m,
            "price": px_now, "currency": "USD" if m == "us" else "HKD" if m == "hk" else "CNY",
            "stmt_currency": last.get("cur"),
            "at": datetime.now(_UTC).isoformat(), "data_at": H.get("at"),
            "cached": bool(H.get("cached")), "stale": bool(H.get("stale")),
            "hist_ok": hist_ok, "last_report": last["end"], "pe_quote": pe_q, "primary": primary,
            "metrics": metrics, "price_band": price_band, "fps_key": fps_key,
            "series": [{"d": p["d"], "px": p["px"], "pe": p["pe"], "pb": p["pb"], "ps": p["ps"]} for p in pts],
            "dcf": dcf, "peers": peers, "overall": overall, "score": score, "tags": tags, "warn": warn,
            "verdict": verdict, "confidence": confidence, "rulers": rulers, "rulers_ok": n_ok,
            "rev_yoy": rev_yoy, "high_growth": high_growth, "peer_n": peer_n,
            "method": "以 PB 锚换算财报币种市值（币种自动抵消），港股累计→TTM、美股 4 季求和；历史点按披露日对齐；"
                      f"反向 DCF：FCF=OCF−CapEx，10 年期+永续 {TERM_G * 100}%，WACC {r * 100:.1f}%"}


# ---------------------------------------------------------------- 结果预计算（2026-10-15）
# 打开估值页时现算要拉 7 年财报 + 5 年周 K（westock 限频 2.5s/次），冷启动易超时 502。
# 改为夜间预计算整份结果落盘；页面与决策页读结果，失败时保留上一次（last-good）并标注日期。
import threading

_RES_LOCK = threading.Lock()


def _results_file():
    return settings.PANEL_OUT_DIR / "valuation_results.json"


def read_results() -> dict:
    return (read_json(_results_file(), {}) or {}).get("items") or {}


def store_result(code: str, d: dict) -> None:
    """锁内重读合并写回（并发安全）。只覆盖成功或「数据不足」的结果，异常不覆盖 last-good。"""
    with _RES_LOCK:
        doc = read_json(_results_file(), {}) or {}
        items = doc.get("items") or {}
        items[code] = {**d, "precomputed_at": _bj_iso()}
        atomic_write_json(_results_file(), {"updated_at": _bj_iso(), "items": items})


def compact(d: dict) -> dict:
    """决策页 / 一页纸用的精简版（不含周频序列）。"""
    if not d.get("ok"):
        return {"ok": False, "reason": d.get("reason") or d.get("error"), "precomputed_at": d.get("precomputed_at")}
    keep = ("ok", "code", "verdict", "overall", "score", "confidence", "rulers", "rulers_ok", "primary", "high_growth",
            "rev_yoy", "peer_n", "price", "currency", "precomputed_at", "at", "warn", "hist_ok")
    out = {k: d.get(k) for k in keep}
    m = (d.get("metrics") or {}).get(d.get("primary")) or {}
    out["hist"] = {"rank": m.get("rank"), "cur": m.get("cur"), "p50": (m.get("band") or {}).get("p50")}
    dc = d.get("dcf") or {}
    out["dcf"] = {k: dc.get(k) for k in ("ok", "mos", "implied_g", "iv", "rev_cagr", "rev_cagr_years", "reason")}
    pe = d.get("peers") or {}
    lst = [x for x in pe.get("list") or [] if not x.get("missing")]
    self_pe = next((x.get("pe") for x in lst if x.get("self")), None)
    others = [x["pe"] for x in lst if not x.get("self") and (x.get("pe") or 0) > 0]
    out["peers"] = {"prem_pe": pe.get("prem_pe"), "median_pe": (pe.get("median") or {}).get("pe"), "self_pe": self_pe,
                    "n": len(others), "cheaper_than": sum(1 for v in others if self_pe and self_pe > 0 and v > self_pe)}
    return out


def refresh_all(force: bool = False) -> dict:
    """全组合逐只计算（串行：westock 限频）；单只失败保留 last-good。"""
    ok = insufficient = fail = 0
    errors: dict[str, str] = {}
    if not westock.available():
        return {"ok": 0, "fail": 0, "skipped": "westock_unavailable"}
    for st in portfolio.all_stocks():
        code = st.get("code")
        if not code or not st.get("gtimg"):
            continue
        try:
            d = view(code, force)
        except Exception as exc:
            fail += 1
            errors[code] = f"{type(exc).__name__}: {str(exc)[:100]}"
            continue
        if d.get("ok"):
            ok += 1
            store_result(code, d)
        elif d.get("error") == "insufficient":
            insufficient += 1
            store_result(code, d)
        else:
            fail += 1
            errors[code] = str(d.get("reason") or d.get("error"))[:100]
    return {"ok": ok, "insufficient": insufficient, "fail": fail, "errors": errors}
