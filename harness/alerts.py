"""预警规则评估：纯程序零 LLM 成本。命中 → 写 alerts_status.json + SSE + webhook。"""
from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

from .app_settings import read_notify, runtime_secret
from .events import bus
from .store import atomic_write_json, data_path, read_json


def evaluate_rules(rules: list[dict], quotes: dict[str, dict], code_by_gtimg: dict[str, str]) -> list[dict]:
    """quotes 以 gtimg 代码为键（与 rules[].gtimg 对齐）。返回命中清单。"""
    hits: list[dict] = []
    for r in rules:
        q = quotes.get(r.get("gtimg", ""))
        if not q or q.get("price") is None:
            continue
        price, pct = q["price"], q.get("pct")
        t, v = r.get("type"), r.get("value")
        hit = False
        if t == "price_below" and v is not None:
            hit = price <= float(v)
        elif t == "price_above" and v is not None:
            hit = price >= float(v)
        elif t == "day_change_abs" and v is not None and pct is not None:
            hit = abs(pct) >= float(v)
        elif t == "pe_below" and v is not None:
            pe = q.get("pe")
            hit = isinstance(pe, (int, float)) and not isinstance(pe, bool) and math.isfinite(pe) and 0 < pe <= float(v)
        if hit:
            hits.append({
                "code": r.get("code") or code_by_gtimg.get(r.get("gtimg", ""), ""),
                "name": r.get("name", q.get("name", "")),
                "type": t,
                "threshold": v,
                "note": r.get("note", ""),
                "price": price,
                "pct": pct,
                **({"pe": q["pe"]} if t == "pe_below" else {}),
                "quote_time": q.get("time", ""),
                "hit_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
    return hits


def load_status() -> dict:
    return read_json(data_path("alerts_status.json"), {"hits": [], "history": []})


def already_reported(status: dict, hit: dict, cooldown_sec: int = 3600) -> bool:
    """同一规则冷却期内不重复推送。"""
    now = time.time()
    for h in status.get("history", [])[-200:]:
        if (
            h.get("code") == hit["code"]
            and h.get("type") == hit["type"]
            and h.get("threshold") == hit["threshold"]
            and now - h.get("ts", 0) < cooldown_sec
        ):
            return True
    return False


_WEBHOOK_RE = re.compile(r"https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9-]{8,64}\Z")
_CN_TZ = timezone(timedelta(hours=8))
_PROCESS_LOCK = asyncio.Lock()


def _in_quiet_hours(quiet: dict, now: datetime | None = None) -> bool:
    if not quiet.get("enabled"):
        return False
    current = (now or datetime.now(_CN_TZ)).strftime("%H:%M")
    start, end = quiet["start"], quiet["end"]
    return (start <= current < end if start < end else current >= start or current < end) if start != end else True


def _notified(status: dict, hit: dict, seconds: int) -> bool:
    return already_reported({"history": status.get("notifications", [])}, hit, seconds)


async def _process_hits(hits: list[dict]) -> list[dict]:
    """站内命中和 SSE 独立记录；通知开关、静默与冷却只控制企业微信投递。"""
    status = load_status()
    status["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    fresh = []
    for hit in hits:
        if already_reported(status, hit):
            continue
        entry = {**hit, "ts": time.time(), "status": "pending_review"}
        fresh.append(entry)
    if fresh:
        status["hits"] = fresh + status.get("hits", [])[:50]
        status.setdefault("history", []).extend(fresh)
        status["history"] = status["history"][-500:]
        status["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    atomic_write_json(data_path("alerts_status.json"), status)
    if fresh:
        bus.publish("alert", fresh)

    notify = read_notify()
    webhook_url = runtime_secret("ALERT_WEBHOOK_URL")
    if (not hits or not notify["enabled"] or not notify["channels"]["wecom"]["enabled"]
            or _in_quiet_hours(notify["quiet"]) or not _WEBHOOK_RE.fullmatch(webhook_url)):
        return fresh
    selected = [h for h in hits if not _notified(status, h, notify["cooldown_min"] * 60)]
    if not selected:
        return fresh
    text = "\n".join(
        f"[预警] {h['name']}({h['code']}) {h['type']} 阈值{h['threshold']}，"
        + (f"当前PE {h['pe']}，现价{h['price']}" if h['type'] == 'pe_below'
           else f"现价{h['price']}（{h.get('pct')}%）")
        for h in selected
    )
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = await client.post(webhook_url, json={"msgtype": "text", "text": {"content": text}})
            response.raise_for_status()
            # 企业微信还可能用 HTTP 200 返回业务错误。只解析状态，不保存原文。
            result = response.json()
            if not isinstance(result, dict) or result.get("errcode") != 0:
                return fresh
    except (httpx.HTTPError, ValueError, TypeError):
        return fresh
    status.setdefault("notifications", []).extend(
        {"code": h["code"], "type": h["type"], "threshold": h["threshold"], "ts": time.time()}
        for h in selected
    )
    status["notifications"] = status["notifications"][-500:]
    atomic_write_json(data_path("alerts_status.json"), status)
    return fresh


async def process_hits(hits: list[dict]) -> list[dict]:
    async with _PROCESS_LOCK:
        return await _process_hits(hits)
