"""数据新鲜度监控（移植 8899 freshness.js，规则适配 harness 自有调度器）。

按「原更新计划」推算每个数据源最近一次应更新的时点（due），与实际时间戳比较，
超过宽限期仍未更新即判逾期（late）。规则可被 data/config/freshness_rules.json 按 id 覆盖。
所有时间按北京时间（UTC+8）计算。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import settings
from .store import read_json

_BJ = timezone(timedelta(hours=8))
_WD = [1, 2, 3, 4, 5]

# 与 harness/scheduler.py 的 cron 保持一致
DEFAULT_RULES = [
    {"id": "analysis", "name": "行情信号与新闻", "file": "analysis.json", "ts": "generated_at",
     "sched": {"freq": "weekly", "days": _WD, "at": ["08:40", "16:20", "21:15"]}, "grace_min": 150,
     "task": "面板数据重建（panel-build）", "tabs": ["news"], "pages": "动态 · 组合总览"},
    {"id": "fundamentals", "name": "基本面指标", "file": "fundamentals.json", "ts": "generated_at",
     "sched": {"freq": "weekly", "days": _WD, "at": ["08:40", "16:20", "21:15"]}, "grace_min": 150,
     "task": "面板数据重建（panel-build）", "tabs": ["fund", "valuation"], "pages": "公司总览 · 估值"},
    {"id": "research", "name": "研报评级与目标价", "file": "research.json", "ts": "generated_at",
     "sched": {"freq": "weekly", "days": _WD, "at": ["08:40", "16:20", "21:15"]}, "grace_min": 150,
     "task": "面板数据重建（panel-build）", "tabs": ["research"], "pages": "研报"},
    {"id": "events", "name": "事件日历", "file": "events.json", "ts": "generated_at",
     "sched": {"freq": "weekly", "days": _WD, "at": ["08:40", "16:20", "21:15"]}, "grace_min": 150,
     "task": "面板数据重建（panel-build）", "tabs": ["news"], "pages": "动态 · 预警与日历"},
    {"id": "valuation", "name": "估值（三把尺子）", "file": "valuation_results.json", "ts": "updated_at",
     "sched": {"freq": "weekly", "days": [1, 2, 3, 4, 5, 6, 7], "at": ["06:30"]}, "grace_min": 240,
     "task": "估值预计算（valuation-refresh）", "tabs": ["valuation", "decide"], "pages": "估值 · 决策"},
    {"id": "earnings", "name": "财报数据", "file": "earnings.json", "ts": "generated_at",
     "sched": {"freq": "weekly", "days": [5], "at": ["16:40"]}, "grace_min": 360,
     "task": "周五全量重建（panel-build-full）", "tabs": ["earn"], "pages": "财报"},
    {"id": "onepager", "name": "公司一页纸", "file": "onepager.json", "ts": "generated_at",
     "sched": {"freq": "weekly", "days": [5], "at": ["16:40"]}, "grace_min": 360,
     "task": "周五全量重建（panel-build-full）", "tabs": ["fund"], "pages": "公司总览"},
    {"id": "themes", "name": "主题聚类", "file": "themes.json", "ts": "generated_at",
     "sched": {"freq": "weekly", "days": [5], "at": ["16:40"]}, "grace_min": 360,
     "task": "周五全量重建（panel-build-full）", "tabs": [], "pages": "主题页", "soft": True},
    {"id": "experts", "name": "大师分析", "file": "experts.json", "ts": "experts_min",
     "sched": {"freq": "weekly", "days": [6], "at": ["09:00"]}, "grace_min": 360,
     "task": "大师分析周更（experts-refresh）", "tabs": ["expert"], "pages": "大师"},
    {"id": "review", "name": "周度复盘", "file": "reviews", "ts": "review_latest",
     "sched": {"freq": "weekly", "days": [6], "at": ["09:30"]}, "grace_min": 240,
     "task": "周度复盘（review-weekly）", "tabs": [], "pages": "周度复盘"},
    {"id": "alerts", "name": "盯盘预警检查", "file": "alerts_status.json", "ts": "checked_at",
     "sched": {"freq": "hourly", "days": _WD, "from": "09:00", "to": "23:00", "every_min": 60},
     "grace_min": 90, "task": "盯盘预警检查（alert-check）", "tabs": [], "pages": "预警与日历"},
]

def _reviews_dir() -> Path:
    return settings.PANEL_OUT_DIR / "reviews"


def _parse_ts(v: Any) -> float | None:
    """解析数据里的时间戳为 UTC 秒；无时区的按北京时间。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f / 1000 if f > 1e12 else f
    s = str(v).strip()
    if re.search(r"[zZ]$|[+\-]\d{2}:?\d{2}$", s):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if not m:
        return None
    try:
        dt = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0), int(m[5] or 0), int(m[6] or 0), tzinfo=_BJ)
        return dt.timestamp()
    except ValueError:
        return None


def _fmt(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, _BJ).strftime("%Y-%m-%d %H:%M")


def _hm(s: str) -> int:
    p = str(s).split(":")
    return int(p[0]) * 60 + int(p[1] or 0)


def _slots(sched: dict, now: float, lookback_days: int) -> list[float]:
    """[now-lookback, now] 内全部计划时点（UTC 秒，升序）。"""
    out: list[float] = []
    if sched.get("freq") == "interval":
        return out
    days = sched.get("days") or [0, 1, 2, 3, 4, 5, 6]
    days = [d % 7 for d in days]  # 8899: 0=周日；Python weekday: 0=周一 → 比较时转换
    bj_now = datetime.fromtimestamp(now, _BJ)
    base = datetime(bj_now.year, bj_now.month, bj_now.day, tzinfo=_BJ)
    for k in range(lookback_days, -1, -1):
        d0 = base - timedelta(days=k)
        dow_js = (d0.weekday() + 1) % 7  # 转成 JS 口径（0=周日）
        if dow_js not in days:
            continue
        if sched.get("freq") == "hourly":
            mins = list(range(_hm(sched.get("from", "00:00")), _hm(sched.get("to", "23:59")) + 1,
                              int(sched.get("every_min") or 60)))
        else:
            mins = [_hm(x) for x in (sched.get("at") or [])]
        for m in mins:
            t = (d0 + timedelta(minutes=m)).timestamp()
            if t <= now:
                out.append(t)
    return out


def _sched_text(s: dict) -> str:
    dn = "日一二三四五六"
    days = s.get("days") or []
    dd = "每天" if len(days) == 7 else ("工作日" if days == _WD else "每周" + "、".join(dn[d] for d in days))
    if s.get("freq") == "interval":
        return f"每 {round((s.get('every_min') or 60) / 60)} 小时"
    if s.get("freq") == "hourly":
        every = s.get("every_min") or 60
        return f"{dd} {s.get('from')}–{s.get('to')} 每 " + (f"{every // 60} 小时" if every >= 60 else f"{every} 分钟")
    return dd + " " + " / ".join(s.get("at") or [])


def _rules() -> list[dict]:
    ov = read_json(settings.CONFIG_DIR / "freshness_rules.json", None)
    omap = {r["id"]: r for r in ((ov or {}).get("rules") or []) if isinstance(r, dict) and r.get("id")}
    merged = []
    for r in DEFAULT_RULES:
        m = dict(r)
        m.update(omap.get(r["id"]) or {})
        merged.append(m)
    for rid, r in omap.items():
        if all(x["id"] != rid for x in DEFAULT_RULES) and r.get("file"):
            merged.append(r)
    return [r for r in merged if r.get("enabled") is not False]


def _file_for(rule_file: str) -> Path:
    """alerts_status.json 在 data/ 根；reviews 是目录；其余在 data/panel/。"""
    if rule_file == "alerts_status.json":
        return settings.DATA_DIR / rule_file
    if rule_file == "experts.json":
        return settings.DATA_DIR / rule_file
    if rule_file == "reviews":
        return _reviews_dir()
    return settings.PANEL_OUT_DIR / rule_file


def _actual_ts(r: dict, holdings_codes: list[str]) -> tuple[float | None, int]:
    """返回 (实际时间戳, 缺失持仓数)。"""
    if r["ts"] == "review_latest":
        best = None
        if _reviews_dir().is_dir():
            for f in _reviews_dir().iterdir():
                if re.fullmatch(r"\d{4}-W\d{2}\.json", f.name):
                    t = _parse_ts((read_json(f, {}) or {}).get("generated_at"))
                    if t and (best is None or t > best):
                        best = t
        return best, 0
    j = read_json(_file_for(r["file"]), None)
    if not isinstance(j, dict):
        return None, 0
    if r["ts"] == "experts_min":
        # 取持仓股里「最旧」的一份；只统计个股级大师（组合层如纳瓦尔不参与个股新鲜度）
        from . import portfolio as _pf
        stock_ids = {p.get("id") for p in _pf.load_personas() if (p.get("scope") or "stock") == "stock"}
        stocks = j.get("stocks") or {}
        min_ts, miss = None, 0
        for c in holdings_codes:
            m = stocks.get(c) or {}
            ts = [t for t in (_parse_ts((m.get(pid) or {}).get("generated_at")) for pid in stock_ids) if t]
            if not ts:
                miss += 1
                continue
            t = min(ts)
            if min_ts is None or t < min_ts:
                min_ts = t
        return min_ts, miss
    return _parse_ts(j.get(r["ts"])), 0


def evaluate(now: float | None = None, holdings_codes: list[str] | None = None) -> dict:
    now = now or time.time()
    codes = holdings_codes or []
    items = []
    for r in _rules():
        at, miss_n = _actual_ts(r, codes)
        due = prev_due = first_missed = None
        status, missed = "ok", 0
        grace_ms = r.get("grace_min", 60) * 60
        ss = _slots(r["sched"], now, 15)
        judged = [t for t in ss if t + grace_ms <= now]
        due = ss[-1] if ss else None
        last_judged = judged[-1] if judged else None
        if at is None:
            status = "missing"
        elif last_judged is not None and at < last_judged:
            miss = [t for t in judged if t > at]
            status, missed = "late", len(miss)
            prev_due, first_missed = last_judged, (miss[0] if miss else None)
        elif due is not None and at < due:
            status = "running"  # 已到点、仍在宽限期内
        if miss_n and status == "ok":
            status = "partial"
        late_by = (now - first_missed) if (status == "late" and first_missed) else None
        level = ("red" if status == "missing"
                 else "red" if status == "late" and missed >= 2
                 else "amber" if status in ("late", "partial") else "ok")
        future = [t for t in _slots(r["sched"], now + 8 * 86400, 8) if t > now]
        m = re.search(r"（([a-z0-9-]+)）", r.get("task") or "")  # 任务名唯一来源：规则里的「中文（job-id）」
        job = m.group(1) if m else None
        items.append({
            "id": r["id"], "name": r["name"], "file": r.get("file"), "pages": r.get("pages"), "job": job,
            "tabs": r.get("tabs") or [], "task": r.get("task"), "task_alt": r.get("task_alt"),
            "server": bool(r.get("server")), "soft": bool(r.get("soft")),
            "schedule": _sched_text(r["sched"]), "status": status, "level": level, "missed": missed,
            "actual_at": _fmt(at), "due_at": _fmt(prev_due or due),
            "first_missed_at": _fmt(first_missed), "next_at": _fmt(future[0] if future else None),
            "late_hours": round(late_by / 3600, 1) if late_by is not None else None,
            "partial_missing": miss_n,
            "due_key": r["id"] + ":" + (_fmt(prev_due) or "missing"),
        })
    bad = [x for x in items if x["level"] != "ok"]
    return {"ok": True, "now": _fmt(now),
            "summary": {"total": len(items), "late": len(bad),
                        "red": sum(1 for x in bad if x["level"] == "red"),
                        "amber": sum(1 for x in bad if x["level"] == "amber")},
            "items": items}
