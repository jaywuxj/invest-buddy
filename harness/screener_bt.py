"""选股回测（2026-11-03）：每月末用 westock 历史快照（当时可得的估值/财务字段）+ 腾讯月 K，
用与实盘完全相同的 screener.score() 打分，看下个月 / 未来 6 个月的表现。

口径：
- 每月一个独立截面；1 个月前向收益互不重叠，是主要证据；6 个月窗口相互重叠，只作参考（有效样本 = 月数 ÷ 6）。
- 信息系数 IC = 当月「综合分」与「下月收益」的秩相关；t = 均值 ÷ 标准差 × √月数。
- 组合：每月取综合分前 N 名（无硬伤）等权，对比同一候选池等权（剔除选股能力之外的市场涨跌）与指数。
- 前视偏差处理：westock 历史快照的财务字段按报告期末回填（实测 2024-09-30 快照已含 11-13 才披露的三季报），
  回测一律用 3 个月前快照里的财务数据，估值倍数用「当时每股基本面 × 当月价格」重建；实盘不存在此问题。
- 已知偏差：已退市股票拿不到月 K 会被剔除（幸存者偏差，利好回测）；
  无行业中性；未计交易成本（报告里给出换手率，按单边 0.3% 估算成本）。
"""
from __future__ import annotations

import math
import statistics
import time
from datetime import date, timedelta
from pathlib import Path

from . import screener as S
from . import settings
from .store import atomic_write_json, read_json

TOP_N = 10
LAG_M = 3        # 财务字段滞后月数：数据源按「报告期末」回填（2024-09-30 快照已含 11-13 才披露的三季报），实测确认
FUND = ("roe", "npm", "gpm", "revg", "npg")


def pit_snapshot(cur: dict, lag: dict, bars: dict, d: str, dl: str) -> dict:
    """消除前视偏差：财务字段取 LAG_M 个月前的快照（届时已披露），估值倍数用「当时的每股基本面 × 今天的价格」重建。
    市值、价格类字段仍用当月（价格本身没有前视问题）。"""
    out = {}
    for ws, x in cur.items():
        y = {k: v for k, v in x.items() if k not in FUND and k not in ("pe", "pb", "ps", "dy")}
        L = lag.get(ws)
        b = bars.get(ws) or []
        i, j = S._idx_at(b, d), S._idx_at(b, dl)
        if L and i >= 0 and j >= 0 and b[j]["c"] > 0:
            ratio = b[i]["c"] / b[j]["c"]
            for k in FUND:
                y[k] = L.get(k)
            for k in ("pe", "pb", "ps"):
                if L.get(k) is not None:
                    y[k] = L[k] * ratio            # 每股盈利/净资产/营收不变，价格变了
            if L.get("dy") is not None:
                y["dy"] = L["dy"] / ratio
        out[ws] = y
    return out


def _out_file() -> Path:
    return settings.PANEL_OUT_DIR / "screener_backtest.json"


def _snap_file(mk: str, ym: str) -> Path:
    d = S._dir() / "snaps"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{mk}-{ym}.json"


def month_ends(n_back: int = 33, today: date | None = None) -> list[str]:
    """最近 n_back 个「已有下一个完整月」的月末工作日（YYYY-MM-DD）。"""
    t = today or date.today()
    y, m = t.year, t.month
    out = []
    for _ in range(n_back + 1):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        d = nxt - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        out.append(d.isoformat())
    return sorted(out)[:-1]             # 最近一个月还没有完整的下一月，丢掉


def get_snapshot(mk: str, d: str, log=print) -> dict:
    f = _snap_file(mk, d[:7])
    hit = read_json(f, None)
    if hit and hit.get("rows"):
        return hit["rows"]
    last = None
    for back in range(4):                 # 月末遇假期取不到时往前挪
        dd = (date.fromisoformat(d) - timedelta(days=back)).isoformat()
        try:
            rows = S.snapshot(mk, dd)
            if len(rows) >= 30:
                atomic_write_json(f, {"date": dd, "n": len(rows), "rows": rows})
                log(f"快照 {mk} {dd}：{len(rows)} 只")
                return rows
        except Exception as e:
            last = e
            time.sleep(8)
    raise RuntimeError(f"{mk} {d} 快照失败：{last}")


def _spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 10:
        return None
    ra, rb = S._pct(dict(enumerate(a))), S._pct(dict(enumerate(b)))
    xs, ys = [ra[i] for i in range(len(a))], [rb[i] for i in range(len(b))]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else None


def _tstat(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    if len(xs) < 3:
        return None
    sd = statistics.stdev(xs)
    return statistics.fmean(xs) / sd * math.sqrt(len(xs)) if sd else None


def _r(v, n=2):
    return None if v is None else round(v, n)


def _fwd(bars: list[dict], i: int, k: int, ym: str) -> float | None:
    """前向收益，要求 k 个月后那根月 K 恰好是第 k 个自然月（停牌缺月则放弃）。"""
    if i < 0 or i + k >= len(bars):
        return None
    y, m = int(ym[:4]), int(ym[5:7]) + k
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    if bars[i + k]["d"][:7] != f"{y:04d}-{m:02d}":
        return None
    return (bars[i + k]["c"] / bars[i]["c"] - 1) * 100


def run(markets: tuple[str, ...] = ("hk", "us"), months: int = 33, log=print) -> dict:
    t0 = time.time()
    all_dates = month_ends(months + LAG_M)
    dates = all_dates[LAG_M:]
    out = {"generated_at": S.bj_now(), "method": __doc__.strip(), "top_n": TOP_N, "weights": S.WEIGHTS, "lag_months": LAG_M,
           "from": dates[0], "to": dates[-1], "markets": {}}
    prev = read()                                       # 只重跑部分市场时保留另一个市场的结果
    if prev.get("from") == out["from"] and prev.get("to") == out["to"]:
        out["markets"] = {k: v for k, v in (prev.get("markets") or {}).items() if k not in markets}
    for mk in markets:
        snaps = {d: get_snapshot(mk, d, log) for d in all_dates}
        wss = sorted({w for s in snaps.values() for w in s} | {S.MARKETS[mk]["bench"]})
        log(f"{mk} 月 K：{len(wss)} 只")
        bars = S.load_month_bars(wss, max_age_h=24 * 7)
        bench = bars.get(S.MARKETS[mk]["bench"]) or []
        months_rows, ic = [], {k: [] for k in ("score", *S.WEIGHTS)}
        top_ex, top_ex6, tag_ex = [], [], {t: [] for t in S.TAG_DEF}
        prev_top: set[str] = set()
        turns = []
        curve = {"top": 1.0, "pool": 1.0, "bench": 1.0}
        series = []
        for n_i, d in enumerate(dates):
            dl = all_dates[n_i]                          # LAG_M 个月前
            rows = S.score(S.build_rows(pit_snapshot(snaps[d], snaps[dl], bars, d, dl), bars, d), mk)
            for r in rows:
                b = bars.get(r["ws"]) or []
                r["f1"] = _fwd(b, r["i"], 1, d[:7])
                r["f6"] = _fwd(b, r["i"], 6, d[:7])
            ok = [r for r in rows if r.get("score") is not None and r.get("f1") is not None]
            if len(ok) < 30:
                continue
            pool1 = statistics.fmean(r["f1"] for r in ok)
            ic["score"].append(_spearman([r["score"] for r in ok], [r["f1"] for r in ok]))
            for k in S.WEIGHTS:
                sub = [r for r in ok if r["f"].get(k) is not None]
                ic[k].append(_spearman([r["f"][k] for r in sub], [r["f1"] for r in sub]) if len(sub) >= 30 else None)
            top = [r for r in ok if not any(x["level"] == "hard" for x in r["flags"])][:TOP_N]
            t1 = statistics.fmean(r["f1"] for r in top)
            top_ex.append(t1 - pool1)
            s6 = [r for r in ok if r.get("f6") is not None]
            if len(s6) >= 30:
                t6 = [r for r in top if r.get("f6") is not None]
                if len(t6) >= 5:
                    top_ex6.append(statistics.fmean(r["f6"] for r in t6) - statistics.fmean(r["f6"] for r in s6))
            for tg in S.TAG_DEF:
                xs = [r["f1"] for r in ok if tg in r["tags"]]
                if len(xs) >= 3:
                    tag_ex[tg].append(statistics.fmean(xs) - pool1)
            ids = {r["ws"] for r in top}
            if prev_top:
                turns.append(len(ids - prev_top) / max(1, len(ids)))
            prev_top = ids
            bi = S._idx_at(bench, d)
            b1 = _fwd(bench, bi, 1, d[:7])
            curve["top"] *= 1 + t1 / 100
            curve["pool"] *= 1 + pool1 / 100
            if b1 is not None:
                curve["bench"] *= 1 + b1 / 100
            series.append({"d": d, "n": len(ok), "top": _r(t1), "pool": _r(pool1), "bench": _r(b1),
                           "ic": _r(ic["score"][-1], 3), "cum_top": _r(curve["top"] * 100 - 100, 1),
                           "cum_pool": _r(curve["pool"] * 100 - 100, 1), "cum_bench": _r(curve["bench"] * 100 - 100, 1),
                           "picks": [{"code": r["code"], "name": r["name"], "score": r["score"], "f1": _r(r["f1"], 1)} for r in top]})
        n = len(series)
        facs = {}
        for k, xs in ic.items():
            ys = [x for x in xs if x is not None]
            facs[k] = {"ic": _r(statistics.fmean(ys), 3) if ys else None, "t": _r(_tstat(ys), 2),
                       "pos": _r(sum(1 for y in ys if y > 0) / len(ys) * 100, 0) if ys else None, "n": len(ys)}
        ex_t = _tstat(top_ex)
        mdd, peak, cum = 0.0, 1.0, 1.0
        for e in top_ex:
            cum *= 1 + e / 100
            peak = max(peak, cum)
            mdd = min(mdd, cum / peak - 1)
        turn = statistics.fmean(turns) if turns else None
        summ = {
            "months": n, "top_ex1": _r(statistics.fmean(top_ex)) if top_ex else None, "top_ex1_t": _r(ex_t),
            "win": _r(sum(1 for e in top_ex if e > 0) / len(top_ex) * 100, 0) if top_ex else None,
            "top_ex6": _r(statistics.fmean(top_ex6)) if top_ex6 else None, "n6": len(top_ex6), "n6_eff": round(len(top_ex6) / 6, 1),
            "cum_top": series[-1]["cum_top"] if series else None, "cum_pool": series[-1]["cum_pool"] if series else None,
            "cum_bench": series[-1]["cum_bench"] if series else None, "ex_mdd": _r(mdd * 100, 1),
            "turnover": _r(turn * 100, 0) if turn is not None else None,
            "cost_drag_y": _r(turn * 2 * 0.3 * 12, 1) if turn is not None else None,
        }
        by_year: dict[str, list[float]] = {}
        for s_, e in zip(series, top_ex):
            by_year.setdefault(s_["d"][:4], []).append(e)
        summ["by_year"] = {y: {"ex": _r(statistics.fmean(v)), "n": len(v), "win": _r(sum(1 for x in v if x > 0) / len(v) * 100, 0)}
                           for y, v in sorted(by_year.items())}
        summ["verdict"], summ["notes"] = verdict(facs, summ, mk)
        tags = {t: {"ex1": _r(statistics.fmean(v)) if v else None, "n": len(v), "t": _r(_tstat(v))} for t, v in tag_ex.items()}
        out["markets"][mk] = {"name": S.MARKETS[mk]["name"], "bench_name": S.MARKETS[mk]["bench_name"],
                              "universe": f"市值 ≥ {int(S.MARKETS[mk]['min_mv'] / 1e8)} 亿{S.MARKETS[mk]['cur']}",
                              "factors": facs, "summary": summ, "tags": tags, "series": series}
    out["took_s"] = round(time.time() - t0)
    atomic_write_json(_out_file(), out)
    return out


def verdict(facs: dict, s: dict, mk: str) -> tuple[str, list[str]]:
    """如实给结论：有效 / 弱有效 / 无优势 / 反向。只看不重叠的 1 个月证据做判定。"""
    ic, t = facs["score"]["ic"], facs["score"]["t"]
    ex, ext = s["top_ex1"], s["top_ex1_t"]
    notes = []
    if ic is None or ex is None:
        return "证据不足", ["有效月份太少，无法判断"]
    if t is not None and t <= -2:
        level = "反向"
    elif t is not None and t >= 2 and ext is not None and ext >= 2 and ex > 0:
        level = "有效"
    elif ic > 0 and ((t or 0) >= 1 or (ext or 0) >= 1) and ex > 0:
        level = "弱有效"
    else:
        level = "无优势"
    notes.append(f"综合分与下月收益的秩相关均值 {ic:+.3f}（t={t}），{facs['score']['pos']}% 的月份为正")
    notes.append(f"前 {TOP_N} 名每月平均跑赢同池等权 {ex:+.2f} 个百分点（t={ext}），{s['win']}% 的月份跑赢")
    good = [S.FACTOR_CN[k] for k in S.WEIGHTS if (facs[k]["t"] or 0) >= 2]
    bad = [S.FACTOR_CN[k] for k in S.WEIGHTS if (facs[k]["t"] or 0) <= -2]
    if good:
        notes.append("单因子里显著有效的：" + "、".join(good))
    if bad:
        notes.append("单因子里显著反向的：" + "、".join(bad) + "（先验权重未因此调整，避免对 " + str(s["months"]) + " 个月样本过拟合）")
    yrs = s.get("by_year") or {}
    if yrs:
        neg = [y for y, v in yrs.items() if (v.get("ex") or 0) < 0]
        notes.append("分年度：" + "，".join(f"{y} 年 {v['ex']:+.2f}（{v['n']} 个月）" for y, v in yrs.items()) +
                     ("；" + "、".join(neg) + " 年为负，超额并不稳定" if neg else "；每年都为正"))
    if s.get("cost_drag_y") is not None:
        notes.append(f"前 {TOP_N} 名月换手约 {s['turnover']}%，按单边 0.3% 估算每年成本约 {s['cost_drag_y']} 个百分点，需从超额里扣除")
    notes.append(f"6 个月窗口互相重叠，有效独立样本只有约 {s['n6_eff']} 个，只作参考")
    if mk == "us":
        notes.append("美股选股接口没有成长与市净率字段，美股模型只用价值（E/P、股息）、质量、动量、稳健四项")
    return level, notes


def read() -> dict:
    return read_json(_out_file(), {}) or {}


if __name__ == "__main__":
    import sys
    mks = tuple(a for a in sys.argv[1:] if a in ("hk", "us")) or ("hk", "us")
    r = run(mks, log=lambda m: print(m, flush=True))
    for mk, v in r["markets"].items():
        print(mk, v["summary"]["verdict"], v["summary"])
        for k, f in v["factors"].items():
            print("  ", k, f)
        for n in v["summary"]["notes"]:
            print("   -", n)
