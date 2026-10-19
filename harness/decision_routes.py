"""逻辑卡 / 计划草稿 / 周度复盘详情 API（8899 契约，数据全部由 harness 自产）。

- GET  /api/theses[?code=]   8899 视图：items 带 eval（指标越线/证伪/安全边际/复查到期）+ drafts + metrics 定义
- POST /api/theses           op: save / falsifier / reviewed / delete（theses.mutate_theses）
- POST /api/thesis/draft     AI 起草逻辑卡（LLM → 规则兜底，落草稿库待用户确认）
- POST /api/plan/quick       计划 Tab 低门槛 AI（judgment/plan 简版草稿，不落盘）
- GET  /api/review?id=2026-W39  周度复盘正文；?code= 旧契约保留 501 诚实降级
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import review as review_mod
from . import theses, thesis_draft
from .app_settings import model_for

_MAX_BODY = 64 * 1024


def _local_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    s = urlsplit(origin)
    return s.scheme == request.url.scheme and s.hostname == request.url.hostname and s.port == request.url.port


async def _body(request: Request) -> dict:
    if not _local_origin(request):
        raise _HttpErr(403, "禁止跨站写入")
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        raise _HttpErr(413, "请求体过大")
    try:
        v = json.loads(raw or b"{}")
    except (ValueError, UnicodeError):
        raise _HttpErr(400, "请求体须为 JSON 对象")
    if not isinstance(v, dict):
        raise _HttpErr(400, "请求体须为 JSON 对象")
    return v


class _HttpErr(Exception):
    def __init__(self, status: int, msg: str):
        self.status, self.msg = status, msg


async def _quotes() -> dict:
    """逻辑卡评估用实时行情（gtimg，带缓存）；失败时按无行情评估。"""
    from . import portfolio
    from .panel_api import _gtimg
    codes = portfolio.gtimg_codes()
    if not codes:
        return {}
    try:
        return await _gtimg.get_quotes(codes)
    except Exception:
        return {}


def register_decision_routes(app: FastAPI) -> None:
    @app.get("/api/theses")
    async def get_theses(code: str = "") -> JSONResponse:
        view = await theses.theses_view(await _quotes())
        if code:
            item = (view.get("items") or {}).get(code)
            return JSONResponse({**view, "items": {code: item} if item else {}})
        return JSONResponse(view)

    @app.post("/api/theses")
    async def save_thesis(request: Request) -> JSONResponse:
        try:
            body = await _body(request)
        except _HttpErr as exc:
            return JSONResponse({"ok": False, "error": exc.msg}, exc.status)
        r = theses.mutate_theses(body)
        if r.get("ok") and body.get("op") == "save":
            try:
                from . import signals_service
                signals_service.invalidate()
            except Exception:
                pass
        return JSONResponse(r, 200 if r.get("ok") else 400)

    @app.post("/api/thesis/draft")
    async def thesis_draft_api(request: Request) -> JSONResponse:
        try:
            body = await _body(request)
        except _HttpErr as exc:
            return JSONResponse({"ok": False, "error": exc.msg}, exc.status)
        r = await thesis_draft.generate(str(body.get("code") or ""), {"rules": bool(body.get("rules"))})
        return JSONResponse(r, 200 if r.get("ok") else int(r.get("status") or 400))

    @app.post("/api/thesis/draft/discard")
    async def thesis_draft_discard(request: Request) -> JSONResponse:
        try:
            body = await _body(request)
        except _HttpErr as exc:
            return JSONResponse({"ok": False, "error": exc.msg}, exc.status)
        return JSONResponse({"ok": True, "discarded": theses.discard_draft(str(body.get("code") or ""))})

    @app.post("/api/plan/quick")
    async def quick_plan(request: Request) -> JSONResponse:
        try:
            body = await _body(request)
        except _HttpErr as exc:
            return JSONResponse({"ok": False, "error": exc.msg}, exc.status)
        r = await thesis_draft.quick(str(body.get("code") or ""),
                                     {"kind": body.get("kind"), "hint": body.get("hint"),
                                      "rules": bool(body.get("rules"))})
        return JSONResponse(r, 200 if r.get("ok") else int(r.get("status") or 400))

    @app.get("/api/review")
    async def review(id: str = "", code: str = "") -> JSONResponse:
        if id:
            doc = review_mod.read_review(id)
            if not doc:
                return JSONResponse({"ok": False, "error": "not_found"}, 404)
            return JSONResponse(doc)
        # 旧版按个股复盘的契约：尚无足够历史快照，诚实 501
        return JSONResponse({"ok": False, "supported": False,
                             "reason": "尚无足够的历史快照与可核实的复盘结果，暂不提供个股自动复盘"}, 501)
