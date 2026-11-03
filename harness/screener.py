"""选股（2026-11-03）：港股 / 美股多因子打分。实盘与回测共用本文件的 snapshot() + build_rows() + score()。

方法论（先验权重，不按回测结果调参，避免过拟合）：
  价值 30%：盈利收益率 E/P、（港股）B/P 与 S/P、股息率          —— Graham / Fama-French HML
  质量 25%：ROE（封顶 60%）、净利率、毛利率                       —— Novy-Marx 毛利因子 / AQR QMJ
  动量 25%：12-1 个月收益（跳过最近 1 个月）+ 距 12 个月高点     —— Jegadeesh-Titman / George-Hwang
  成长 10%：营收、归母净利同比（仅港股，美股选股接口无此字段）   —— Lynch GARP
  稳健 10%：近 12 个月月度波动率越低越好                         —— 低波动异象
每个因子在「同一市场、同一时点」的候选池里取百分位（0-100），综合分 = 有数据因子的加权平均。

数据：westock screen condition（港股市值 ≥100 亿港元、美股 ≥100 亿美元），按字段分批取、按代码拼接
（多字段写在同一条件里会把缺任一字段的股票整行过滤掉，实测美股只剩 7 只）；价格与动量一律来自腾讯月 K
（--date 历史模式下 ClosePrice 不是当日价格，不能用）。
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import settings
from .providers import westock
from .store import atomic_write_json, read_json

_TZ = timezone(timedelta(hours=8))
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/120 Safari/537.36", "Referer": "https://gu.qq.com/"}

WEIGHTS = {"value": 0.30, "quality": 0.25, "momentum": 0.25, "growth": 0.10, "lowvol": 0.10}
FACTOR_CN = {"value": "价值", "quality": "质量", "momentum": "动量", "growth": "成长", "lowvol": "稳健"}

MARKETS: dict[str, dict] = {
    "hk": {"name": "港股", "min_mv": 100e8, "cur": "HKD", "bench": "hkHSI", "bench_name": "恒生指数",
           "calls": [
               ("PeTTM > -1000000", "Chg20D > -100", "Week52High > 0", "TurnoverRate > -1"),
               ("PbLF > -1000000", "PsTTM > -1000000"),
               ("DivTTM > -1",),
               ("RoeWeighted > -100000", "NetProfitRatio > -1000000"),
               ("GrossIncomeRatio > -100000",),
               ("OperatingRevenueGr1y > -100000", "NpParentCompanyGr1y > -100000000"),
           ]},
    "us": {"name": "美股", "min_mv": 100e8, "cur": "USD", "bench": "us.INX", "bench_name": "标普 500",
           "calls": [
               ("PeTTM > -1000000", "Chg20D > -100", "Week52High > 0", "TurnoverRate > -1"),
               ("DivTTM > -1",),
               ("ROE > -100000",),
               ("NetMargin > -1000000",),
               ("GrossMargin > -100000",),
           ]},
}
COLMAP = {"TotalMV": "mv", "PeTTM": "pe", "PbLF": "pb", "PsTTM": "ps", "DivTTM": "dy",
          "RoeWeighted": "roe", "ROE": "roe", "NetProfitRatio": "npm", "NetMargin": "npm",
          "GrossIncomeRatio": "gpm", "GrossMargin": "gpm", "OperatingRevenueGr1y": "revg",
          "NpParentCompanyGr1y": "npg", "Chg20D": "chg20", "Week52High": "hi52w", "TurnoverRate": "turn",
          "ClosePrice": "px_live"}


def _dir() -> Path:
    d = settings.DATA_DIR / "screener"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cfg() -> dict:
    """可选覆盖：data/config/screener.json（min_mv_yi.hk/us、weights、picks_per_market）。"""
    c = read_json(settings.CONFIG_DIR / "screener.json", {}) or {}
    w = dict(WEIGHTS)
    for k, v in (c.get("weights") or {}).items():
        if k in w and isinstance(v, (int, float)) and 0 <= v <= 1:
            w[k] = float(v)
    return {"weights": w, "picks": int(c.get("picks_per_market") or 5),
            "deep": int(c.get("deep_per_market") or 12),
            "min_mv": {m: float((c.get("min_mv_yi") or {}).get(m) or MARKETS[m]["min_mv"] / 1e8) * 1e8 for m in MARKETS}}


# ---------------------------------------------------------------- 代码清洗

_MLP = re.compile(r"(?i)(\bL\.?P\.?\s*$|\bLp\b|limited partnership)")
_US_JUNK_NAME = re.compile(r"(?i)\b(pfd|preferred|depositary|notes?|cum\b|perp|trust units?|warrants?|% )")


def us_ok(ticker: str, name: str) -> bool:
    """剔除场外 ADR / 优先股 / 权证等：5 位且以 F/Y/P/Q/W/U/R/Z 结尾、名称含优先股等字样。"""
    if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", ticker or ""):
        return False
    if len(ticker) == 5 and ticker[-1] in "FYPQWURZ":
        return False
    return not _US_JUNK_NAME.search(name or "")


def to_entry(ws: str) -> dict | None:
    m = re.fullmatch(r"(hk|us)(.+)", ws or "")
    if not m:
        return None
    if m[1] == "hk":
        if not re.fullmatch(r"\d{5}", m[2]) or m[2].startswith("8"):   # 8xxxx = 人民币柜台 / 创业板
            return None
        return {"ws": ws, "code": m[2] + ".HK", "gtimg": ws, "mk": "hk"}
    t = m[2].upper()
    return {"ws": "us" + t, "code": t + ".US", "gtimg": "us" + t, "mk": "us", "ticker": t}


# ---------------------------------------------------------------- 横截面快照

def _num(v: Any) -> float | None:
    s = str(v if v is not None else "").strip()
    if not s or re.search(r"[%亿万]", s):
        return None
    return westock.num(s)


def _yi(v: float | None) -> float | None:
    if v is None or not math.isfinite(v):
        return None
    return v / 1e8 if abs(v) >= 1e7 else v


def snapshot(mk: str, date: str | None = None, min_mv: float | None = None) -> dict[str, dict]:
    """一个市场在 date（None=实时）的候选池：{ws: {name, mv(亿), pe, ...}}。分批取字段后按代码拼接。"""
    M = MARKETS[mk]
    floor = min_mv or M["min_mv"]
    out: dict[str, dict] = {}
    for i, conds in enumerate(M["calls"]):
        expr = "intersect([" + ", ".join((f"TotalMV > {int(floor)}",) + tuple(conds)) + "])"
        args = ["screen", "condition", "--market", mk, "--expression", expr, "--limit", "3000", "--orderby", "TotalMV"]
        if date:
            args += ["--date", date]
        md = ""
        for k in range(4):
            try:
                md = westock.run(args, timeout=90)
                if "|" in md or "失败" not in md:
                    break
            except Exception:
                md = ""
            time.sleep(5 * (k + 1))
        tb = westock.tables(md)
        rows = tb[0]["rows"] if tb else []
        if i == 0 and not rows:
            raise RuntimeError(f"{M['name']} {date or '实时'} 候选池为空（westock 不可用或限频）")
        for r in rows:
            e = to_entry(str(r.get("code") or ""))
            if not e:
                continue
            name = str(r.get("name") or "")
            if mk == "us" and not us_ok(e["ticker"], name):
                continue
            mv = _yi(_num(r.get("TotalMV")))
            if mv is None or mv < floor / 1e8 * 0.98:          # 列错位行：市值会落在查询区间外
                continue
            if i == 0:
                out[e["ws"]] = {**e, "name": name, "mv": round(mv, 2)}
            elif e["ws"] not in out:
                continue
            x = out[e["ws"]]
            for col, key in COLMAP.items():
                if col in r and col != "TotalMV" and key not in x:
                    x[key] = _num(r.get(col))
    # 同名多份额（GOOG / GOOGL）只留市值大的一份
    seen: dict[str, str] = {}
    for ws, x in sorted(out.items(), key=lambda kv: -(kv[1].get("mv") or 0)):
        k = re.sub(r"\W", "", x["name"]).lower()
        if k in seen:
            out.pop(ws)
        else:
            seen[k] = ws
    return out


# ---------------------------------------------------------------- 月 K（腾讯，前复权）

def _kline_file() -> Path:
    return _dir() / "kline_month.json"


_KM: dict | None = None


def _km() -> dict:
    global _KM
    if _KM is None:
        _KM = read_json(_kline_file(), {}) or {}
    return _KM


def save_kline_cache() -> None:
    if _KM is not None:
        atomic_write_json(_kline_file(), _KM)


def _fetch_month(sym: str, n: int = 48) -> list[dict]:
    from .kline import gtimg_kline_json
    node = (gtimg_kline_json(sym, "month", n).get("data") or {}).get(sym) or {}
    raw = node.get("qfqmonth") or node.get("month") or []
    out = []
    for x in raw:
        try:
            out.append({"d": str(x[0])[:10], "c": float(x[2]), "h": float(x[3])})
        except (IndexError, TypeError, ValueError):
            continue
    return [b for b in out if b["c"] > 0]


class _Blocked(Exception):
    pass


_BLOCK_UNTIL = 0.0


def _fetch_retry(sym: str) -> list[dict]:
    """腾讯接口被限流时返回 501/429：指数退避重试；连续失败则全局暂停，避免越打越封。"""
    global _BLOCK_UNTIL
    for k in range(4):
        wait = _BLOCK_UNTIL - time.time()
        if wait > 0:
            time.sleep(min(wait, 60))
        try:
            b = _fetch_month(sym)
            time.sleep(0.15)
            return b
        except urllib.error.HTTPError as e:
            if e.code in (429, 501, 502, 503):
                _BLOCK_UNTIL = time.time() + 10 * (2 ** k)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2 * (k + 1))
    raise _Blocked(sym)


def month_bars(ws: str, max_age_h: float = 20) -> list[dict]:
    """月 K（含当月未完成的一根）。美股依次尝试 .N / .OQ / .AM 后缀，记住命中的那个。
    网络失败 / 被限流不写缓存（下次重试）；只有「各后缀都确实没有数据」才缓存为空。"""
    km = _km()
    hit = km.get(ws)
    if hit and time.time() - hit.get("at", 0) < max_age_h * 3600 and (hit.get("bars") or hit.get("none")):
        return hit.get("bars") or []
    cands = [ws] if ws.startswith("hk") or "." in ws else (
        [hit["sym"]] if hit and hit.get("sym") else []) + [ws + ".N", ws + ".OQ", ws + ".AM"]
    bars: list[dict] = []
    sym = None
    for s in dict.fromkeys(cands):
        try:
            b = _fetch_retry(s)
        except _Blocked:
            return (hit or {}).get("bars") or []          # 被限流：用旧缓存（若有），不覆盖
        except Exception:
            continue
        if len(b) >= 6:
            bars, sym = b, s
            break
    km[ws] = {"at": time.time(), "sym": sym, "bars": bars, "none": not bars}
    return bars


def load_month_bars(wss: list[str], max_age_h: float = 20, workers: int = 3) -> dict[str, list[dict]]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = dict(zip(wss, ex.map(lambda w: month_bars(w, max_age_h), wss)))
    save_kline_cache()
    return res


def _idx_at(bars: list[dict], date: str) -> int:
    """date 所在月份的那根月 K 下标（月 K 日期为月末或当月最后交易日）。"""
    ym = date[:7]
    for i in range(len(bars) - 1, -1, -1):
        if bars[i]["d"][:7] <= ym:
            return i if bars[i]["d"][:7] == ym else -1
    return -1


def price_feats(bars: list[dict], date: str) -> dict:
    i = _idx_at(bars, date)
    if i < 0:
        return {}
    c = [b["c"] for b in bars]
    out: dict[str, Any] = {"px": c[i], "i": i}
    if i >= 7:
        k = min(12, i)
        out["mom"] = (c[i - 1] / c[i - k] - 1) * 100            # 12-1 个月（不足 12 个月时用已有月份，至少 6 个月）
    if i >= 6:
        hi = max(b["h"] for b in bars[max(0, i - 11): i + 1])
        out["hiprox"] = c[i] / hi * 100 if hi > 0 else None
        rets = [math.log(c[j] / c[j - 1]) for j in range(max(1, i - 11), i + 1)]
        if len(rets) >= 5:
            out["vol"] = statistics.pstdev(rets) * math.sqrt(12) * 100
    return out


def forward(bars: list[dict], i: int, k: int) -> float | None:
    if i < 0 or i + k >= len(bars):
        return None
    return (bars[i + k]["c"] / bars[i]["c"] - 1) * 100


# ---------------------------------------------------------------- 因子打分（实盘与回测共用）

def _pct(vals: dict[str, float | None]) -> dict[str, float]:
    """横截面百分位（0-100，并列取平均名次）；None 不参与。"""
    xs = sorted((v, k) for k, v in vals.items() if v is not None and math.isfinite(v))
    n = len(xs)
    out: dict[str, float] = {}
    if n < 5:
        return out
    i = 0
    while i < n:
        j = i
        while j + 1 < n and xs[j + 1][0] == xs[i][0]:
            j += 1
        p = ((i + j) / 2) / (n - 1) * 100
        for t in range(i, j + 1):
            out[xs[t][1]] = p
        i = j + 1
    return out


def _avg(xs: list[float | None]) -> float | None:
    ys = [x for x in xs if x is not None]
    return sum(ys) / len(ys) if ys else None


def build_rows(snap: dict[str, dict], bars: dict[str, list[dict]], date: str) -> list[dict]:
    rows = []
    for ws, x in snap.items():
        f = price_feats(bars.get(ws) or [], date)
        if not f.get("px"):
            continue                       # 没有可用行情（场外 / 已退市）不进入候选
        rows.append({**x, **f})
    return rows


def score(rows: list[dict], mk: str, weights: dict | None = None) -> list[dict]:
    """给每行加 f（各因子百分位）、score（综合 0-100）、tags、flags。原地修改并按 score 降序返回。"""
    W = weights or WEIGHTS
    ids = [r["ws"] for r in rows]
    by = {r["ws"]: r for r in rows}

    def col(fn):
        return {k: fn(by[k]) for k in ids}

    def inv(v, pos=True):
        return (1 / v * 100) if v is not None and v > 0 else (-1.0 if pos and v is not None else None)

    ep = _pct(col(lambda r: inv(r.get("pe"))))                       # 亏损股 E/P 记为最低
    bp = _pct(col(lambda r: inv(r.get("pb"), False)))
    sp = _pct(col(lambda r: inv(r.get("ps"), False)))
    dy = _pct(col(lambda r: min(r.get("dy") or 0.0, 12.0)))         # 未派息记 0；>12% 视同 12（异常高息不加分）

    def roe(r):
        v = r.get("roe")
        if v is None or v > 100:          # >100% 多为回购 / 负净资产放大，不参与排名
            return None
        return min(v, 60.0)
    q_roe, q_npm, q_gpm = _pct(col(roe)), _pct(col(lambda r: r.get("npm"))), _pct(col(lambda r: r.get("gpm")))
    g_rev = _pct(col(lambda r: max(-80.0, min(r["revg"], 200.0)) if r.get("revg") is not None else None))
    g_np = _pct(col(lambda r: max(-100.0, min(r["npg"], 300.0)) if r.get("npg") is not None else None))
    m_mom, m_hi = _pct(col(lambda r: r.get("mom"))), _pct(col(lambda r: r.get("hiprox")))
    lv = _pct(col(lambda r: -r["vol"] if r.get("vol") is not None else None))
    vol_hi = sorted(v for v in (r.get("vol") for r in rows) if v is not None)
    vol_p90 = vol_hi[int(len(vol_hi) * 0.9)] if len(vol_hi) >= 10 else None

    for r in rows:
        k = r["ws"]
        f = {"value": _avg([ep.get(k), bp.get(k), sp.get(k), dy.get(k)]),
             "quality": _avg([q_roe.get(k), q_npm.get(k), q_gpm.get(k)]),
             "momentum": _avg([m_mom.get(k), m_hi.get(k)]),
             "growth": _avg([g_rev.get(k), g_np.get(k)]) if mk == "hk" else None,
             "lowvol": lv.get(k)}
        num = sum(W[n] * v for n, v in f.items() if v is not None)
        den = sum(W[n] for n, v in f.items() if v is not None)
        r["f"] = {n: (round(v, 1) if v is not None else None) for n, v in f.items()}
        r["score"] = round(num / den, 1) if den >= 0.5 else None    # 价值/质量/动量至少缺两项时不打分
        flags = []
        if r.get("pe") is not None and r["pe"] <= 0:
            flags.append(("hard", "亏损", "滚动市盈率为负：过去 12 个月亏损"))
        elif r.get("pe") is not None and r["pe"] < 3:
            flags.append(("hard", "PE 异常低", f"PE {r['pe']:.1f} 倍，常见于一次性收益，利润不可持续"))
        if mk == "us" and _MLP.search(r.get("name") or ""):
            flags.append(("hard", "合伙制 MLP", "有限合伙结构：非美国投资者分红按最高税率预扣、需 K-1 报税，不适合普通投资者"))
        if r.get("npg") is not None and r["npg"] < -30:
            flags.append(("hard", "利润大跌", f"归母净利同比 {r['npg']:.0f}%，便宜可能是价值陷阱"))
        if r.get("mom") is not None and r["mom"] < -40:
            flags.append(("hard", "仍在急跌", f"过去一年（跳过最近 1 个月）跌 {abs(r['mom']):.0f}%，趋势未止"))
        if r.get("roe") is not None and r["roe"] > 100:
            flags.append(("soft", "ROE 被放大", f"ROE {r['roe']:.0f}%，多为大额回购或净资产为负，质量分未计入"))
        if (r.get("dy") or 0) > 12:
            flags.append(("soft", "股息率异常高", f"股息率 {r['dy']:.1f}%，可能含特别股息或分红不可持续"))
        if vol_p90 and r.get("vol") is not None and r["vol"] >= vol_p90:
            flags.append(("soft", "波动大", f"年化波动 {r['vol']:.0f}%，处于候选池最高 10%"))
        r["flags"] = [{"level": a, "k": b, "why": c} for a, b, c in flags]
        fv, fq, fm, fg = f["value"], f["quality"], f["momentum"], f["growth"]
        tags = []
        if fv is not None and fv >= 80 and (fq or 0) >= 50:
            tags.append("低估优质")
        if fg is not None and fg >= 75 and (fv or 0) >= 40 and (fq or 0) >= 40:
            tags.append("成长合理价")
        if fm is not None and fm >= 80 and (fq or 0) >= 50:
            tags.append("趋势向上")
        if fq is not None and fq >= 85 and (fv or 0) >= 30:
            tags.append("优质复利")
        if fv is not None and fv >= 70 and (r.get("hiprox") or 100) <= 70 and (fq or 0) >= 50 and not any(x["level"] == "hard" for x in r["flags"]):
            tags.append("错杀反转")
        r["tags"] = tags
    rows.sort(key=lambda r: -(r["score"] if r.get("score") is not None else -1))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


TAG_DEF = {
    "低估优质": "价值分 ≥80 且质量分 ≥50：便宜，同时生意不差（格雷厄姆 + 巴菲特）",
    "成长合理价": "成长分 ≥75、价值与质量分都 ≥40：增长快但价格不离谱（林奇 GARP，仅港股）",
    "趋势向上": "动量分 ≥80 且质量分 ≥50：基本面过得去、股价已在走强（动量 + 质量）",
    "优质复利": "质量分 ≥85 且价值分 ≥30：高回报率的好生意，价格不算太贵",
    "错杀反转": "价值分 ≥70、质量分 ≥50，股价距 12 个月高点回撤 ≥30%、且没有硬伤（马克斯逆向）",
}


def bj_now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")
