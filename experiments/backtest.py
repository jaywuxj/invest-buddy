"""实验一：与 TradingAgents（arXiv:2412.20138）同协议的回测对标。
- 标的：AAPL / GOOGL / AMZN（主表，与原文一致）；扩展：+ NVDA / MSFT / META
- 区间：2024-01-02 ~ 2024-03-28（原文 2024-01-01 ~ 2024-03-29 的全部交易日）；扩展窗口 2024-01-02 ~ 2026-09-30
- 规则：t 日收盘产生信号，t+1 日起持有（无未来函数）；只做多/空仓；不计交易成本（与原文一致）
- 指标：CR、ARR（标准年化：(1+CR)^(252/N)-1）、SR（日超额收益年化，Rf=5.25%/年，3 个月美债）、MDD
- 基线由本文按原文描述复现：Buy&Hold、MACD(12,26,9)、KDJ&RSI、ZMR(20 日 z 分数均值回归)、SMA(5/20)
- 本系统：PF = 三因子价格合成信号（signals_engine.price_factors + combine，与线上同一份代码）
          PF-Hi = 仅在高置信度看多时入场（conf ≥ 0.6），看空时离场
"""
import json, os, math, sys, urllib.parse, urllib.request
from pathlib import Path
import pathlib

sys.path.insert(0, os.environ.get("HARNESS_ROOT", str(pathlib.Path(__file__).resolve().parent.parent)))
from harness import signals_engine as SIG  # noqa: E402

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SYM = {"AAPL": "usAAPL.OQ", "GOOGL": "usGOOGL.OQ", "AMZN": "usAMZN.OQ",
       "NVDA": "usNVDA.OQ", "MSFT": "usMSFT.OQ", "META": "usMETA.OQ"}
RF = 0.0525


def fetch(sym, start, end):
    out, s = {}, start
    for y0, y1 in (("2022-06-01", "2023-12-31"), ("2024-01-01", "2025-06-30"), ("2025-07-01", "2026-10-15")):
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + urllib.parse.quote(sym)
               + f",day,{y0},{y1},640,qfq")
        d = json.loads(OP.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read())
        node = list(d["data"].values())[0]
        for r in node.get("qfqday") or node.get("day") or []:
            out[r[0]] = {"d": r[0], "o": float(r[1]), "c": float(r[2]), "h": float(r[3]), "l": float(r[4])}
    rows = [out[k] for k in sorted(out)]
    return split_adjust(rows)


def split_adjust(rows):
    """行情源前复权未覆盖拆股：检测单日 ≤ -75% 且前收/今收接近整数倍的跳变，向前回溯除以拆股比例。"""
    for i in range(len(rows) - 1, 0, -1):
        ratio = rows[i - 1]["c"] / rows[i]["c"]
        k = round(ratio)
        if ratio > 4 and abs(ratio - k) / k < 0.15:
            for j in range(i):
                for f in ("o", "c", "h", "l"):
                    rows[j][f] /= k
    return rows


def ema(a, n):
    k, e, r = 2 / (n + 1), None, []
    for x in a:
        e = x if e is None else x * k + e * (1 - k)
        r.append(e)
    return r


def rsi(c, i, n=14):
    if i < n:
        return 50.0
    g = l = 0.0
    for j in range(i - n + 1, i + 1):
        d = c[j] - c[j - 1]
        g, l = (g + d, l) if d > 0 else (g, l - d)
    return 50.0 if g + l == 0 else 100 * g / (g + l)


def kdj_k(rows, n=9):
    K, out = 50.0, []
    for i in range(len(rows)):
        lo = min(r["l"] for r in rows[max(0, i - n + 1): i + 1])
        hi = max(r["h"] for r in rows[max(0, i - n + 1): i + 1])
        rsv = 50.0 if hi == lo else (rows[i]["c"] - lo) / (hi - lo) * 100
        K = 2 / 3 * K + rsv / 3
        out.append(K)
    return out


def positions(rows, name):
    """返回每日目标仓位（t 日收盘决定，作用于 t→t+1 的收益）。"""
    c = [r["c"] for r in rows]
    n = len(c)
    pos = [0.0] * n
    if name == "B&H":
        return [1.0] * n
    if name == "MACD":
        m = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
        s = ema(m, 9)
        return [1.0 if m[i] > s[i] else 0.0 for i in range(n)]
    if name == "SMA":
        out = []
        for i in range(n):
            if i < 20:
                out.append(0.0); continue
            s5 = sum(c[i - 4:i + 1]) / 5; s20 = sum(c[i - 19:i + 1]) / 20
            out.append(1.0 if s5 > s20 else 0.0)
        return out
    if name == "KDJ&RSI":
        K, p = kdj_k(rows), 0.0
        for i in range(n):
            r = rsi(c, i)
            if p == 0 and K[i] < 20 and r < 40:
                p = 1.0
            elif p == 1 and (K[i] > 80 or r > 70):
                p = 0.0
            pos[i] = p
        return pos
    if name == "ZMR":
        p = 0.0
        for i in range(n):
            if i >= 20:
                w = c[i - 19:i + 1]; mu = sum(w) / 20
                sd = (sum((x - mu) ** 2 for x in w) / 20) ** 0.5 or 1e-9
                z = (c[i] - mu) / sd
                if p == 0 and z < -1:
                    p = 1.0
                elif p == 1 and z > 0:
                    p = 0.0
            pos[i] = p
        return pos
    if name in ("PF", "PF-Hi"):
        p = 0.0
        for i in range(n):
            if i >= 60:
                cmb = SIG.combine(SIG.price_factors(c, i))
                if cmb["label"] == "看多" and (name == "PF" or (cmb["conf"] or 0) >= 0.6):
                    p = 1.0
                elif cmb["label"] == "看空":
                    p = 0.0
            pos[i] = p
        return pos
    raise KeyError(name)


def evaluate(rows, pos, d0, d1):
    idx = [i for i, r in enumerate(rows) if d0 <= r["d"] <= d1]
    i0, i1 = idx[0], idx[-1]
    rets = []
    for i in range(i0, i1):
        r = rows[i + 1]["c"] / rows[i]["c"] - 1
        rets.append(pos[i] * r)
    eq, peak, mdd, curve = 1.0, 1.0, 0.0, [1.0]
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
        curve.append(eq)
    N = len(rets)
    cr = eq - 1
    arr = (1 + cr) ** (252 / N) - 1 if N else 0
    rf_d = RF / 252
    ex = [r - rf_d for r in rets]
    mu = sum(ex) / N
    sd = (sum((r - sum(rets) / N) ** 2 for r in rets) / (N - 1)) ** 0.5 if N > 1 else 0
    sr = mu / sd * math.sqrt(252) if sd > 0 else 0.0
    trades = sum(1 for i in range(i0 + 1, i1) if pos[i] != pos[i - 1])
    exposure = sum(pos[i] for i in range(i0, i1)) / N
    return {"CR": cr * 100, "ARR": arr * 100, "SR": sr, "MDD": mdd * 100, "N": N,
            "trades": trades, "exposure": exposure * 100, "curve": curve,
            "dates": [rows[i]["d"] for i in range(i0, i1 + 1)]}


METHODS = ["B&H", "MACD", "KDJ&RSI", "ZMR", "SMA", "PF", "PF-Hi"]
data = {t: fetch(s, "2022-06-01", "2026-10-15") for t, s in SYM.items()}
out = {"protocol": {}, "extended": {}}
for t in SYM:
    rows = data[t]
    P = {m: positions(rows, m) for m in METHODS}
    if t in ("AAPL", "GOOGL", "AMZN"):
        out["protocol"][t] = {m: evaluate(rows, P[m], "2024-01-01", "2024-03-29") for m in METHODS}
    out["extended"][t] = {m: evaluate(rows, P[m], "2024-01-01", "2026-09-30") for m in METHODS}
    print(t, len(rows), rows[0]["d"], rows[-1]["d"])

for sec in ("protocol", "extended"):
    print("\n==", sec)
    for t, res in out[sec].items():
        print(t, " | ".join(f"{m}: CR {r['CR']:.2f} SR {r['SR']:.2f} MDD {r['MDD']:.2f}" for m, r in res.items()))
json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False)
