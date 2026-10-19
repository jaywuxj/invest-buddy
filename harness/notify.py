"""推送中心：统一的通知发送 / 日志 / 配置（对齐 8899 server.js 的 notify 契约）。

- 通道：企业微信机器人（macOS 系统通知在独立版未接入，状态固定 skip:off）；
- 日志：data/panel/notify_log.json，最新在前，最多 200 条；
- 去重：同一 key 在冷却窗口内已送达则跳过；红色级别不受静默时段限制；
- 配置：notify.json 由 app_settings.read_notify() 统一读取，本模块只补充日志与推送。
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

import httpx

from .app_settings import read_notify, runtime_secret, set_section
from .kline import bj_full, bj_now
from . import settings
from .store import atomic_write_json, read_json

def _log_file() -> Path:
    return settings.PANEL_OUT_DIR / "notify_log.json"
_LOG_MAX = 200
_WEBHOOK_RE = re.compile(r"https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9-]{8,64}\Z")
_SEND_LOCK = asyncio.Lock()


def read_log() -> dict:
    d = read_json(_log_file(), {"items": []})
    return d if isinstance(d.get("items"), list) else {"items": []}


def _in_quiet(cfg: dict) -> bool:
    quiet = cfg.get("quiet") or {}
    if not quiet.get("enabled"):
        return False
    hm = bj_now().strftime("%H:%M")
    s, e = str(quiet.get("start") or ""), str(quiet.get("end") or "")
    if not re.fullmatch(r"\d{1,2}:\d{2}", s) or not re.fullmatch(r"\d{1,2}:\d{2}", e):
        return False
    return s <= hm < e if s <= e else (hm >= s or hm < e)


async def _send_wecom(url: str, title: str, text: str, level: str) -> str:
    prefix = {"red": "🔴 ", "amber": "🟡 "}.get(level, "")
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            r = await client.post(url, json={"msgtype": "text", "text": {"content": prefix + title + "\n" + text}})
            r.raise_for_status()
            j = r.json()
            if not isinstance(j, dict) or j.get("errcode") != 0:
                return "err:wecom_errcode"
            return "ok"
    except Exception as exc:
        return "err:" + str(exc)[:60]


async def notify(opts: dict) -> dict:
    """{title, text, level=red|amber|info, keys[], key, dedupe_hours, source, force}"""
    cfg = read_notify()
    title = str(opts.get("title") or "投资Buddy")[:80]
    text = str(opts.get("text") or "")[:1500]
    level = opts.get("level") if opts.get("level") in ("red", "amber", "info") else "info"
    keys = [str(k) for k in ([opts.get("key")] if opts.get("key") else []) + list(opts.get("keys") or []) if k]
    force = bool(opts.get("force"))
    if not cfg.get("enabled") and not force:
        return {"ok": True, "skipped": "disabled"}
    log = read_log()
    if keys and not force:
        win = (float(opts.get("dedupe_hours") * 3600) if opts.get("dedupe_hours") is not None
               else float(cfg.get("cooldown_min", 60)) * 60)
        now = time.time()
        for it in log["items"]:
            if not it.get("delivered"):
                continue
            try:
                ts = float(it.get("ts_epoch") or 0)
            except (TypeError, ValueError):
                ts = 0
            if now - ts < win and any(k in (it.get("keys") or []) for k in keys):
                return {"ok": True, "skipped": "cooldown"}
    quiet = (not force) and level != "red" and _in_quiet(cfg)
    results: dict[str, str] = {}
    if quiet:
        results = {"macos": "skip:quiet", "wecom": "skip:quiet"}
    else:
        results["macos"] = "skip:off"  # 独立版未接入 macOS 通知中心
        url = runtime_secret("ALERT_WEBHOOK_URL")
        if not (cfg.get("channels") or {}).get("wecom", {}).get("enabled"):
            results["wecom"] = "skip:off"
        elif not url or not _WEBHOOK_RE.fullmatch(url):
            results["wecom"] = "skip:unconfigured"
        else:
            results["wecom"] = await _send_wecom(url, title, text, level)
    delivered = any(v == "ok" for v in results.values())
    item = {"id": uuid.uuid4().hex[:10], "ts_epoch": time.time(), "at": bj_full(),
            "title": title, "text": text, "level": level,
            "source": str(opts.get("source") or "manual")[:30], "keys": keys,
            "results": results, "delivered": delivered}
    async with _SEND_LOCK:
        log = read_log()
        log["items"] = [item] + log["items"][: _LOG_MAX - 1]
        atomic_write_json(_log_file(), log)
    return {"ok": True, "delivered": delivered, "results": results, "item": item}


def _masked_webhook() -> dict:
    url = runtime_secret("ALERT_WEBHOOK_URL")
    if not url:
        return {"configured": False}
    m = re.search(r"key=([A-Za-z0-9-]{4})[A-Za-z0-9-]+([A-Za-z0-9-]{4})", url)
    masked = ("****" + m[1] + "…" + m[2]) if m else "已配置"
    return {"configured": True, "masked": masked}


def status(watch: dict | None = None) -> dict:
    """/api/notify/status 契约：{config, watch, wecom, log}"""
    cfg = read_notify()
    return {"ok": True, "config": cfg, "watch": watch or {},
            "wecom": _masked_webhook(), "log": read_log()["items"][:50]}


def update_config(body: dict) -> dict:
    """对齐 8899 的扁平字段（enabled/macos/wecom/risk_digest/cooldown_min/quiet/wecom_url）。"""
    if not isinstance(body, dict):
        return {"ok": False, "error": "bad_body"}
    value: dict[str, Any] = {}
    for k in ("enabled", "macos", "wecom", "risk_digest", "cooldown_min"):
        if k in body:
            value[k] = body[k]
    if isinstance(body.get("quiet"), dict):
        value["quiet"] = body["quiet"]
    try:
        if value:
            set_section("notify", value)
        if body.get("wecom_url") is not None:
            set_section("secrets", {"wecom_url": str(body["wecom_url"]).strip()})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}
