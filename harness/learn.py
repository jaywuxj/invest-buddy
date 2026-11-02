"""投资学堂（2026-11-02）：内容校验、本机学习进度、外链检查。

- 内容源 harness/learn_content/lessons.json + links.json 随代码发布；进度存 data/learn_progress.json（只在本机）。
- 外链只能来自 links.json 白名单（模型不生成 URL）；`python -m harness.learn --check` 逐条检查可达性。
- 进度文件损坏/符号链接 → 抛错且绝不覆盖（与其他用户记录同一铁律）。
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import settings
from .store import atomic_write_json

_DIR = Path(__file__).resolve().parent / "learn_content"
_TZ = timezone(timedelta(hours=8))
_LOCK = threading.Lock()
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_GO_STOCK = {"news", "decide", "fund", "earn", "valuation", "expert", "notes"}
_GO_OV = {"ov-signal", "ov-diag", "ov-pf", "ov-risk", "ov-trades", "ov-acc"}
LIVE_KEYS = {"pe-pct", "mos", "peers", "confidence", "sizing", "hhi", "exposure", "funds", "thesis", "deep-loss"}


class LearnError(ValueError):
    pass


def _stamp() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def load_content() -> dict:
    """读取并校验内容；任何引用错误都直接报错（宁可不上线，也不显示坏链接/坏跳转）。"""
    lessons = json.loads((_DIR / "lessons.json").read_text(encoding="utf-8"))
    links = json.loads((_DIR / "links.json").read_text(encoding="utf-8")).get("items") or {}
    for lid, it in links.items():
        if not _ID.match(lid):
            raise LearnError(f"外链 id 不合法：{lid}")
        if not str(it.get("url") or "").startswith(("https://", "http://")):
            raise LearnError(f"外链 {lid} 缺少 http(s) URL")
        if not it.get("title"):
            raise LearnError(f"外链 {lid} 缺少标题")
    seen: set[str] = set()
    for p in lessons.get("paths") or []:
        if not _ID.match(p.get("id", "")):
            raise LearnError(f"路径 id 不合法：{p.get('id')}")
        for ls in p.get("lessons") or []:
            lid = ls.get("id", "")
            if not _ID.match(lid) or lid in seen:
                raise LearnError(f"课程 id 不合法或重复：{lid}")
            seen.add(lid)
            for k in ("title", "one", "product", "pitfall"):
                if not ls.get(k):
                    raise LearnError(f"课程 {lid} 缺少 {k}")
            for x in ls.get("links") or []:
                if x not in links:
                    raise LearnError(f"课程 {lid} 引用了不存在的外链 {x}")
            go = ls.get("go")
            if go and not ((go.get("stock") in _GO_STOCK) or (go.get("ov") in _GO_OV)):
                raise LearnError(f"课程 {lid} 的跳转目标不合法：{go}")
            if ls.get("live") and ls["live"] not in LIVE_KEYS:
                raise LearnError(f"课程 {lid} 的 live 钩子未实现：{ls['live']}")
    return {"version": lessons.get("version"), "paths": lessons.get("paths") or [], "links": links,
            "total": len(seen), "ids": seen}


def _progress_file() -> Path:
    return settings.DATA_DIR / "learn_progress.json"


def load_progress() -> dict:
    f = _progress_file()
    if not f.exists():
        return {"done": {}, "last": None, "updated_at": None}
    if f.is_symlink():
        raise LearnError("learn_progress.json 是符号链接；已停止读取并保留原件")
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        raise LearnError("learn_progress.json 无法解析；已停止读取并保留原件") from None
    if not isinstance(d, dict) or not isinstance(d.get("done", {}), dict):
        raise LearnError("learn_progress.json 结构异常")
    d.setdefault("done", {})
    d.setdefault("last", None)
    return d


def mutate_progress(body: dict) -> dict:
    ids = load_content()["ids"]
    lid = str(body.get("id") or "")
    if lid not in ids:
        raise LearnError("课程不存在")
    op = body.get("op") or "done"
    with _LOCK:
        d = load_progress()                       # 锁内重读再合并
        if op == "done":
            if body.get("done") is False:
                d["done"].pop(lid, None)
            else:
                d["done"][lid] = _stamp()[:10]
        elif op == "last":
            d["last"] = lid
        else:
            raise LearnError("不支持的操作")
        d["done"] = {k: v for k, v in d["done"].items() if k in ids}   # 课程下线后自动清理
        d["updated_at"] = _stamp()
        atomic_write_json(_progress_file(), d)
        return d


def check_links(sleep: float = 1.2, timeout: float = 15, only: str | None = None) -> list[dict]:
    """逐条请求外链（限速，避免被站点限流）；返回 [{id, url, status, ok}]。"""
    import httpx
    links = load_content()["links"]
    out = []
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome/120 Safari/537.36"}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as c:
        for lid, it in links.items():
            if only and only not in lid:
                continue
            try:
                # 维基百科要求脚本使用可识别的 UA（伪装浏览器反而 403）
                h = {"User-Agent": "InvestBuddy-LinkCheck/1.0 (https://github.com/jaywuxj/invest-buddy)"} \
                    if "wikipedia.org" in it["url"] else None
                r = c.get(it["url"], headers=h)
                status = r.status_code
            except Exception as exc:
                status = "ERR " + type(exc).__name__
            out.append({"id": lid, "url": it["url"], "status": status,
                        "ok": isinstance(status, int) and status < 400})
            time.sleep(sleep)
    return out


def register_learn_routes(app: FastAPI) -> None:
    @app.get("/api/learn")
    async def get_learn():
        try:
            c = load_content()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": "content_invalid", "message": str(exc)[:200]}, status_code=500)
        try:
            prog = load_progress()
        except LearnError as exc:
            return JSONResponse({"ok": False, "error": "progress_corrupt", "message": str(exc)}, status_code=500)
        return {"ok": True, "version": c["version"], "paths": c["paths"], "links": c["links"],
                "total": c["total"], "progress": prog}

    @app.post("/api/learn/progress")
    async def post_learn_progress(body: dict[str, Any]):
        try:
            return {"ok": True, "progress": mutate_progress(body)}
        except LearnError as exc:
            return JSONResponse({"ok": False, "error": "bad_request", "message": str(exc)}, status_code=400)


if __name__ == "__main__":
    if "--check" in sys.argv:
        bad = 0
        only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
        for r in check_links(only=only):
            bad += not r["ok"]
            print(("ok  " if r["ok"] else "BAD ") + str(r["status"]).ljust(8) + r["id"].ljust(22) + r["url"])
        print(f"共 {len(load_content()['links'])} 条，异常 {bad} 条")
        sys.exit(1 if bad else 0)
    c = load_content()
    print(f"内容校验通过：{len(c['paths'])} 条路径、{c['total']} 课、{len(c['links'])} 条外链")
