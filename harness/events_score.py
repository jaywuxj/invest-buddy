"""事件重要性评分（移植 8899 events_score.js）。

- 规则层：timeline_engine.rule_score（与前端 timeline.js 同口径），1-5 分；
- LLM 层：按簇 ID 缓存到 data/panel/event_scores.json，LLM 分覆盖规则分并保留 rule_imp；
  评分用设置里的事件场景模型与 prompt（events_system/events_rubric）；
- 推送层：近 3 天出现 ≥4 分事件时推送一次（notified 去重，30 天滚动）。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from . import portfolio, timeline_engine as TL
from .kline import bj_date
from . import settings
from .store import atomic_write_json, read_json

_DIRS = ("正面", "负面", "中性")
_HORIZONS = ("短期", "中期", "长期")
def _file() -> Path:
    return settings.PANEL_OUT_DIR / "event_scores.json"


def _clip(s: Any, n: int) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()[:n]


def _dnum(s: str) -> float:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() / 86400
    except ValueError:
        return float("nan")


def sanitize(x: dict, src: str) -> dict | None:
    try:
        imp = int(x.get("imp"))
    except (TypeError, ValueError):
        return None
    if not 1 <= imp <= 5:
        return None
    return {"imp": imp,
            "direction": x.get("direction") if x.get("direction") in _DIRS else "中性",
            "horizon": x.get("horizon") if x.get("horizon") in _HORIZONS else "中期",
            "chain": _clip(x.get("chain"), 80), "why": _clip(x.get("why"), 160),
            "src": src or "llm", "scored_at": datetime.now(timezone.utc).isoformat()}


def _read() -> dict:
    d = read_json(_file(), None)
    return d if isinstance(d, dict) and isinstance(d.get("items"), dict) else {"items": {}, "notified": {}}


def _write(d: dict) -> None:
    d["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(_file(), d)


def scores_for(code: str) -> dict:
    return (_read().get("items") or {}).get(code) or {}


def _timeline_src(code: str) -> dict:
    analysis = read_json(settings.PANEL_OUT_DIR / "analysis.json", {}) or {}
    events = read_json(settings.PANEL_OUT_DIR / "events.json", {}) or {}
    earnings = read_json(settings.PANEL_OUT_DIR / "earnings.json", {}) or {}
    return {"news": analysis.get("news") or [], "events": events, "earnings": earnings,
            "today": bj_date(), "scores": scores_for(code)}


def build_timeline(code: str) -> dict:
    return TL.build(code, _timeline_src(code))


def recent(days: int, code: str = "") -> list[dict]:
    t0 = _dnum(bj_date())
    out = []
    stocks = portfolio.all_stocks()
    for s in stocks:
        if code and s.get("code") != code:
            continue
        tl = build_timeline(s["code"])
        for c in tl.get("items", []):
            dd = _dnum(c.get("date") or "")
            if dd == dd and t0 - dd <= days:
                out.append({"code": s["code"], "name": s.get("name"), **c})
    return out


def _llm_prompts() -> tuple[str, str]:
    try:
        from .app_settings import prompt_for
        return prompt_for("events_system"), prompt_for("events_rubric")
    except Exception:
        return ("你是买方研究员，负责给上市公司事件做重要性分级。只输出 JSON 数组，不要解释。",
                "常规回购、例行分红、媒体重复报道一般不超过 3 分；监管处罚、业绩大幅偏离预期、重大并购/制裁可到 4-5 分。")


async def score_llm(opts: dict | None = None) -> dict:
    opts = opts or {}
    from .app_settings import model_for
    if not model_for("events").get("key"):
        return {"ok": False, "error": "未配置模型 API Key，保持规则评分"}
    d = _read()
    d.setdefault("items", {})
    days = min(90, int(opts.get("days") or 30))
    limit = min(20, int(opts.get("limit") or 20))
    pool = recent(days, str(opts.get("code") or ""))
    if not opts.get("force"):
        pool = [c for c in pool if c["id"] not in (d["items"].get(c["code"]) or {})]
    pool.sort(key=lambda c: (-(c.get("imp") or 0), c.get("date") or ""), reverse=False)
    pool.sort(key=lambda c: (c.get("date") or ""), reverse=True)
    pool.sort(key=lambda c: -(c.get("imp") or 0))
    batch = pool[:limit]
    if not batch:
        return {"ok": True, "scored": 0, "pending": 0, "note": f"近 {days} 天事件均已评分"}
    sys_p, rubric = _llm_prompts()
    lines = []
    for i, c in enumerate(batch):
        lines.append(f"{i}. [{c.get('name')}] {c.get('date')} 「{_clip(c['main'].get('title'), 60)}」 "
                     f"类别:{c.get('category')} 来源:{'/'.join(c.get('kinds') or [])} 报道数:{c.get('size')}"
                     + (f" 摘要:{_clip(c['main'].get('body'), 80)}" if c["main"].get("body") else ""))
    user = ("为下列事件打分。字段：i(序号), imp(1-5：5=改变投资逻辑/显著影响估值，4=影响未来 1-2 个季度盈利或风险溢价，"
            "3=值得跟踪，2=常规，1=噪音), direction(正面/负面/中性), horizon(短期/中期/长期), "
            "chain(影响传导链，不超过 30 字，用 → 连接), why(评分理由，不超过 50 字)。\n注意：" + rubric
            + "\n\n" + "\n".join(lines))
    from . import llm
    ans = await llm.chat("flash", sys_p, user, temperature=0.1, max_tokens=2000, timeout=60, skill="panel-events")
    m = re.search(r"\[[\s\S]*\]", ans.get("text") or "")
    if not m:
        return {"ok": False, "error": "LLM 未返回 JSON 数组"}
    import json
    try:
        arr = json.loads(m[0])
    except ValueError:
        return {"ok": False, "error": "LLM JSON 解析失败"}
    n = 0
    for x in arr if isinstance(arr, list) else []:
        try:
            c = batch[int(x.get("i"))]
        except (TypeError, ValueError, IndexError):
            continue
        v = sanitize(x, "llm")
        if not v:
            continue
        d["items"].setdefault(c["code"], {})[c["id"]] = {**v, "title": _clip(c["main"].get("title"), 40),
                                                          "date": c.get("date")}
        n += 1
    _write(d)
    return {"ok": True, "scored": n, "pending": max(0, len(pool) - len(batch)), "batch": len(batch)}


def import_scores(body: dict) -> dict:
    arr = body.get("items") if isinstance(body.get("items"), list) else []
    d = _read()
    d.setdefault("items", {})
    known = {s.get("code"): 1 for s in portfolio.all_stocks()}
    n, bad = 0, 0
    for x in arr[:200]:
        v = known.get(x.get("code")) and x.get("id") and sanitize(x, _clip(body.get("src") or "import", 20))
        if not v:
            bad += 1
            continue
        d["items"].setdefault(x["code"], {})[str(x["id"])[:80]] = {
            **v, "title": _clip(x.get("title"), 40), "date": _clip(x.get("date"), 10)}
        n += 1
    _write(d)
    return {"ok": n > 0, "imported": n, "rejected": bad}


def high(days: int = 3, min_imp: int = 4) -> list[dict]:
    return [{"code": c["code"], "name": c.get("name"), "id": c["id"], "date": c.get("date"),
             "imp": c["imp"], "title": c["main"].get("title"), "category": c.get("category"),
             "sentiment": c.get("sentiment"), "chain": c["score"].get("chain"),
             "horizon": c["score"].get("horizon"), "src": c["score"].get("src"),
             "url": c["main"].get("url") or ""}
            for c in sorted((c for c in recent(days) if c.get("imp", 0) >= min_imp),
                            key=lambda c: (-c["imp"], c.get("date") or ""), reverse=False)]


def stats() -> dict:
    d = _read()
    n = llm = 0
    for m in (d.get("items") or {}).values():
        for v in m.values():
            n += 1
            if v.get("src") != "rule":
                llm += 1
    return {"cached": n, "llm": llm, "updated_at": d.get("updated_at")}


async def alert_tick() -> dict:
    hs = high(3, 4)
    if not hs:
        return {"ok": True, "sent": 0}
    d = _read()
    d.setdefault("notified", {})
    fresh = [x for x in hs if f"{x['code']}|{x['id']}" not in d["notified"]]
    if not fresh:
        return {"ok": True, "sent": 0}
    from .notify import notify
    text = "\n".join(f"★{x['imp']} {x.get('name')}｜{_clip(x['title'], 40)}（{x.get('sentiment')}，{x.get('chain')}）"
                     for x in fresh[:8])
    r = await notify({"title": f"高重要性事件 {len(fresh)} 条", "text": text,
                      "level": "red" if any(x["imp"] >= 5 or x.get("sentiment") == "利空" for x in fresh) else "amber",
                      "keys": [f"evt:{x['code']}:{x['id']}" for x in fresh], "source": "events_score"})
    today = bj_date()
    for x in fresh:
        d["notified"][f"{x['code']}|{x['id']}"] = today
    t0 = _dnum(today)
    for k in list(d["notified"]):
        if t0 - _dnum(d["notified"][k]) > 30:
            del d["notified"][k]
    _write(d)
    return {"ok": True, "sent": len(fresh), "notify": r}
