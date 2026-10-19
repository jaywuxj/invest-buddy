"""每周组合复盘（移植 8899 server.js 的 buildReview/listReviews/defaultWeek）。

纯规则计算：gtimg 前复权日 K 周收益 + 实时汇率折人民币 + 面板本地数据
（诊断 / 信号 / 逻辑卡 / 预警日志 / 计划 / 财报日历）。产物：data/panel/reviews/{id}.md + .json。
"""
from __future__ import annotations

import asyncio
import math
import re
from typing import Any

from . import notify as notify_mod
from . import portfolio, theses
from .kline import (bj_date, bj_full, default_week, fetch_fx, fetch_kline,
                    kline_sym_of, shift_date, week_from_id)
from . import settings
from .store import atomic_write_json, read_json

def _reviews_dir() -> Path:
    return settings.PANEL_OUT_DIR / "reviews"
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


def _sgn(v: float, d: int = 2) -> str:
    return ("+" if v > 0 else "") + f"{v:.{d}f}"


def _yuan(v: float) -> str:
    return ("-" if v < 0 else "+" if v > 0 else "") + "¥" + f"{round(abs(v)):,}"


def list_reviews() -> list[dict]:
    if not _reviews_dir().is_dir():
        return []
    out = []
    for f in sorted(_reviews_dir().iterdir(), reverse=True):
        if re.fullmatch(r"\d{4}-W\d{2}\.json", f.name):
            j = read_json(f, None)
            if isinstance(j, dict):
                out.append(j)
    return out


def read_review(wid: str) -> dict | None:
    if not WEEK_RE.fullmatch(wid or ""):
        return None
    md = _reviews_dir() / f"{wid}.md"
    if not md.exists():
        return None
    meta = read_json(_reviews_dir() / f"{wid}.json", None)
    return {"ok": True, "id": wid, "md": md.read_text(encoding="utf-8"), "meta": meta}


def _week_change(sym: str, wk: dict, n: int) -> dict | None:
    bars = fetch_kline(sym, n)
    base = next((x for x in reversed(bars) if x["d"] < wk["monday"]), None)
    end = next((x for x in reversed(bars) if x["d"] <= wk["sunday"]), None)
    if not base or not end or end["d"] < wk["monday"]:
        return None
    return {"base": base["c"], "base_d": base["d"], "end": end["c"], "end_d": end["d"],
            "pct": (end["c"] / base["c"] - 1) * 100}


async def build_review(wid: str | None = None, opts: dict | None = None) -> dict:
    opts = opts or {}
    wk = week_from_id(wid) if wid else default_week()
    if not wk:
        return {"ok": False, "error": "bad_week_id（格式 2026-W39）"}
    today = bj_date()
    if wk["monday"] > today:
        return {"ok": False, "error": "该周尚未开始"}
    n = max(40, math.ceil((datetime_to_ts(today) - datetime_to_ts(wk["monday"])) / 86400) + 20)
    h = portfolio.load_holdings()
    fx = await asyncio.to_thread(fetch_fx)
    pos = [p for p in (h.get("positions") or [])
           if isinstance(p.get("cost"), (int, float)) and isinstance(p.get("shares"), (int, float)) and p.get("shares")]

    rows, missing = [], []
    async def one(p: dict) -> None:
        try:
            c = await asyncio.to_thread(_week_change, kline_sym_of(p.get("gtimg", ""), p), wk, n)
            if not c:
                missing.append(p.get("name"))
                return
            rate = fx["rates"].get(p.get("currency"), 1)
            rows.append({"code": p["code"], "name": p.get("name"), "pct": c["pct"],
                         "mv0": c["base"] * p["shares"] * rate, "mv1": c["end"] * p["shares"] * rate,
                         "end_d": c["end_d"]})
        except Exception:
            missing.append(p.get("name"))
    await asyncio.gather(*(one(p) for p in pos))
    for r in rows:
        r["contrib"] = r["mv1"] - r["mv0"]
    rows.sort(key=lambda r: -r["contrib"])
    mv0, mv1 = sum(r["mv0"] for r in rows), sum(r["mv1"] for r in rows)
    week_pnl = mv1 - mv0
    week_pct = week_pnl / mv0 * 100 if mv0 else 0
    bench: dict[str, float | None] = {}
    for label, sym in (("恒生指数", "hkHSI"), ("纳斯达克", "us.IXIC"), ("标普500", "us.INX")):
        try:
            c = await asyncio.to_thread(_week_change, sym, wk, n)
            bench[label] = round(c["pct"], 2) if c else None
        except Exception:
            bench[label] = None
    by_pct = sorted(rows, key=lambda r: -r["pct"])

    from .portfolio_routes import portfolio_diagnosis
    diag = await portfolio_diagnosis()
    an = read_json(settings.PANEL_OUT_DIR / "analysis.json", {}) or {}
    tv = await theses.theses_view()
    log = [it for it in notify_mod.read_log()["items"]
           if wk["monday"] <= str(it.get("at") or "")[:10] <= wk["sunday"]
           and it.get("source") not in ("test", "review")]
    horizon = shift_date(max(today, wk["sunday"]), 14)
    plans_doc = read_json(settings.DATA_DIR / "plans.json", {}) or {}
    plans = sorted((p for p in (plans_doc.get("plans") or [])
                    if p.get("status") == "open" and p.get("deadline") and p["deadline"] <= horizon),
                   key=lambda p: str(p["deadline"]))
    ev = read_json(settings.PANEL_OUT_DIR / "events.json", {}) or {}
    earn = [e for e in (ev.get("upcoming_earnings") or []) if today <= e.get("date", "") <= horizon]

    L: list[str] = []
    L += [f"# 周度复盘 · {wk['id']}（{wk['monday'][5:]} ~ {wk['sunday'][5:]}）", ""]
    L += [f"> 生成于 {bj_full()} · 数据：gtimg 前复权日 K 收盘 + 实时汇率"
          f"（HKD {fx['rates'].get('HKD')} / USD {fx['rates'].get('USD')}）+ 面板本地数据 · 规则计算，不构成投资建议", ""]
    L += ["## 一、本周结果", "", "| 指标 | 数值 |", "| --- | --- |"]
    L.append(f"| 组合周收益 | **{_sgn(week_pct)}%**（{_yuan(week_pnl)}，{len(rows)} 只持仓） |")
    for k, v in bench.items():
        L.append(f"| {k} | {'--' if v is None else _sgn(v) + '%'} |")
    totals = diag.get("totals") or {}
    summary = diag.get("summary") or {}
    L.append(f"| 当前组合市值 | ¥{(totals.get('mv') or 0) / 1e4:.2f} 万 · 累计浮盈亏 {_sgn(totals.get('pnl_pct') or 0)}% |")
    L.append(f"| 红线体检 | 超限 {summary.get('red', 0)} 项 / 预警 {summary.get('amber', 0)} 项 |")
    L.append("")
    if missing:
        L += ["缺 K 线未计入：" + "、".join(m for m in missing if m), ""]
    L += ["## 二、贡献排名", "", "| 标的 | 周涨跌 | 贡献 | 贡献 pp |", "| --- | --- | --- | --- |"]
    for r in rows:
        L.append(f"| {r['name']} | {_sgn(r['pct'])}% | {_yuan(r['contrib'])} | {_sgn(r['contrib'] / mv0 * 100 if mv0 else 0)} |")
    L.append("")
    sigs = an.get("signals") or []
    L += [f"## 三、信号面（周更 {an.get('generated_at') or '--'}）", ""]
    for lv, nm in (("bullish", "偏多持有"), ("normal", "持有"), ("neutral", "观望"), ("watch-close", "审视")):
        g = [s for s in sigs if s.get("action_level") == lv]
        if g:
            L.append("- **" + nm + "**：" + "、".join(s.get("name", "") for s in g))
    L.append("")
    th_items = list((tv.get("items") or {}).values())
    no_thesis = [p.get("name") for p in pos if p["code"] not in (tv.get("items") or {})]
    falsified: list[str] = []
    breach_t: list[str] = []
    due_t: list[dict] = []
    for t in th_items:
        for f in (t.get("falsifiers") or []):
            if f.get("hit"):
                falsified.append(f"{t.get('name')}：{f.get('text')}")
        ev_t = t.get("eval") or {}
        for i, m in enumerate(t.get("metrics") or []):
            em = (ev_t.get("metrics") or [{}])[i] if i < len(ev_t.get("metrics") or []) else {}
            if em.get("breach"):
                breach_t.append(f"{t.get('name')}：{m.get('label')} {em.get('cur')}（阈值 {m.get('op')} {m.get('value')}）")
        if t.get("review_date") and t["review_date"] <= horizon:
            due_t.append(t)
    L += ["## 四、投资逻辑卡", ""]
    L.append(f"- 已建 {len(th_items)} 张；持仓未建：{'、'.join(no_thesis) if no_thesis else '无'}")
    L.append("- 证伪命中：" + ("" if falsified else "无"))
    L += ["  - " + x for x in falsified]
    L.append("- 跟踪指标越线：" + ("" if breach_t else "无"))
    L += ["  - " + x for x in breach_t]
    L.append("")
    L += ["## 五、本周预警与推送回顾", ""]
    if not log:
        L.append("本周无预警或红线推送。")
    for it in reversed(log):
        L.append(f"- {str(it.get('at'))[5:16]} {it.get('title')}" + ("" if it.get("delivered") else "（未送达）"))
    L.append("")
    todos: list[str] = []
    for x in (diag.get("breaches") or []):
        if x.get("level") == "red":
            todos.append(f"红线处置 · {x.get('dim')}：{x.get('title')}")
    for p in plans:
        todos.append(f"计划 · {p.get('name')}：{p.get('title')}（截止 {p['deadline']}"
                     + ("，已逾期" if p["deadline"] < today else "") + "）")
    for e in earn:
        todos.append(f"财报 · {e.get('name')} {e.get('period')}（{e.get('date')}）：复核逻辑卡跟踪指标")
    for t in due_t:
        todos.append(f"逻辑卡复查 · {t.get('name')}（{t.get('review_date')}）")
    if no_thesis:
        todos.append("补建逻辑卡 · " + "、".join(no_thesis[:5]) + (f" 等 {len(no_thesis)} 只" if len(no_thesis) > 5 else ""))
    L += [f"## 六、下周待办（截至 {horizon}）", ""]
    L.append("暂无。" if not todos else "")
    L += ["- [ ] " + x for x in todos]
    L.append("")
    prev = read_review(wk["id"])
    comment = str(opts["comment"])[:4000].strip() if opts.get("comment") is not None else ((prev or {}).get("meta") or {}).get("comment") or ""
    L += ["## 七、复盘点评", "", comment or "（可在页面「周度复盘」重新生成或手工补充点评）", ""]

    meta = {"id": wk["id"], "range": {"from": wk["monday"], "to": wk["sunday"]}, "generated_at": bj_full(),
            "week_pct": round(week_pct, 2), "week_pnl": round(week_pnl), "bench": bench,
            "best": {"name": by_pct[0]["name"], "pct": round(by_pct[0]["pct"], 2)} if by_pct else None,
            "worst": {"name": by_pct[-1]["name"], "pct": round(by_pct[-1]["pct"], 2)} if by_pct else None,
            "top_contrib": {"name": rows[0]["name"], "pnl": round(rows[0]["contrib"])} if rows else None,
            "alerts_n": len(log), "falsified_n": len(falsified), "breach_n": len(breach_t),
            "todo_n": len(todos), "red": summary.get("red", 0), "amber": summary.get("amber", 0),
            "missing": missing, "comment": comment}
    _reviews_dir().mkdir(parents=True, exist_ok=True)
    (_reviews_dir() / f"{wk['id']}.md").write_text("\n".join(L), encoding="utf-8")
    atomic_write_json(_reviews_dir() / f"{wk['id']}.json", meta)
    pushed = None
    if opts.get("notify") is not False:
        pushed = await notify_mod.notify({
            "title": f"周度复盘 · {wk['id']} 组合 {_sgn(week_pct)}%",
            "text": (f"周收益 {_yuan(week_pnl)} ｜ 恒指 {'--' if bench['恒生指数'] is None else _sgn(bench['恒生指数']) + '%'}"
                     f" · 纳指 {'--' if bench['纳斯达克'] is None else _sgn(bench['纳斯达克']) + '%'}"
                     f"\n最强 {meta['best']['name'] + ' ' + _sgn(meta['best']['pct']) + '%' if meta['best'] else '--'}"
                     f" · 最弱 {meta['worst']['name'] + ' ' + _sgn(meta['worst']['pct']) + '%' if meta['worst'] else '--'}"
                     f"\n预警 {meta['alerts_n']} 次 · 证伪 {meta['falsified_n']} 项 · 待办 {meta['todo_n']} 项，详见面板「周度复盘」。"),
            "level": "info", "keys": ["review:" + wk["id"]], "dedupe_hours": 12, "source": "review"})
    return {"ok": True, "id": wk["id"], "meta": meta,
            "pushed": (pushed or {}).get("results") or (pushed or {}).get("skipped")}


def datetime_to_ts(ds: str) -> float:
    from datetime import datetime
    return datetime.strptime(ds[:10], "%Y-%m-%d").timestamp()
