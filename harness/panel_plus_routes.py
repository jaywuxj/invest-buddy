"""8899 能力对齐的补齐路由：新鲜度 / 事件评分 / 纪律检查 / 周度复盘 / 共识 / 准确率 / 推送 / 研报全文。

全部数据来自 harness 自有链路（data/panel、data/config、gtimg 行情），不读 8899/WorkBuddy 产物。
"""
from __future__ import annotations

import asyncio
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import accuracy as accuracy_mod
from . import discipline as discipline_mod
from . import events_score
from . import freshness as freshness_mod
from . import notify as notify_mod
from . import portfolio, review as review_mod, signals_service, theses, thesis_draft
from .app_settings import auto_enabled, read_rules
from .kline import bj_date, bj_now, fetch_kline, kline_sym_of
from .settings import DATA_DIR
from .store import read_json

_CODE_RE = re.compile(r"^[0-9A-Za-z.]{1,20}$")
_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


def _local_origin(request: Request) -> bool:
    from urllib.parse import urlsplit
    origin = request.headers.get("origin")
    if not origin:
        return True
    s = urlsplit(origin)
    return s.scheme == request.url.scheme and s.hostname == request.url.hostname and s.port == request.url.port


async def _body(request: Request) -> dict:
    if not _local_origin(request):
        return {"__error__": (403, "forbidden_origin")}
    raw = await request.body()
    if len(raw) > 64 * 1024:
        return {"__error__": (413, "请求体过大")}
    import json
    try:
        v = json.loads(raw or b"{}")
    except (ValueError, UnicodeError):
        return {"__error__": (400, "请求体须为 JSON 对象")}
    return v if isinstance(v, dict) else {"__error__": (400, "请求体须为 JSON 对象")}


def _market_sessions() -> dict:
    b = bj_now()
    wk = 1 <= b.isoweekday() <= 5
    hm = b.hour * 60 + b.minute
    hk = wk and ((570 <= hm < 720) or (780 <= hm < 970))
    us = (wk and hm >= 1290) or (2 <= b.isoweekday() <= 6 and hm < 300)
    return {"hk": bool(hk), "us": bool(us)}


def _watch_status() -> dict:
    st = read_json(DATA_DIR / "alerts_status.json", {}) or {}
    hits = st.get("hits") or []
    return {"enabled": auto_enabled("alert_watch"), "interval_sec": 3600,
            "sessions": _market_sessions(),
            "last_check": st.get("checked_at"), "last_hits": len(hits),
            "last_error": None}


def register_plus_routes(app: FastAPI) -> None:
    @app.get("/api/freshness")
    async def api_freshness(now: str = "") -> JSONResponse:
        import time as _t
        override = None
        if now:
            ts = freshness_mod._parse_ts(now)  # noqa: SLF001（调试参数与 8899 一致）
            override = ts
        codes = [s["code"] for s in portfolio.positions()]
        return JSONResponse(freshness_mod.evaluate(override or _t.time(), codes))

    @app.get("/api/events/scores")
    async def api_event_scores() -> JSONResponse:
        d = events_score._read()  # noqa: SLF001
        return JSONResponse({"ok": True, "items": d.get("items") or {},
                             "stats": events_score.stats()})

    @app.post("/api/events/score")
    async def api_event_score(request: Request) -> JSONResponse:
        body = await _body(request)
        if "__error__" in body:
            return JSONResponse({"ok": False, "error": body["__error__"][1]}, body["__error__"][0])
        code = str(body.get("code") or "")
        if code and not _CODE_RE.fullmatch(code):
            return JSONResponse({"ok": False, "error": "bad_code"}, 400)
        r = await events_score.score_llm({"code": code, "days": body.get("days"), "force": bool(body.get("force"))})
        return JSONResponse(r, 200 if r.get("ok") else 400)

    @app.post("/api/events/score/import")
    async def api_event_score_import(request: Request) -> JSONResponse:
        body = await _body(request)
        if "__error__" in body:
            return JSONResponse({"ok": False, "error": body["__error__"][1]}, body["__error__"][0])
        return JSONResponse(events_score.import_scores(body))

    @app.post("/api/discipline/check")
    async def api_discipline_check(request: Request) -> JSONResponse:
        body = await _body(request)
        if "__error__" in body:
            return JSONResponse({"ok": False, "error": body["__error__"][1]}, body["__error__"][0])
        code = str(body.get("code") or "")
        st = portfolio.stock_meta(code)
        if not st:
            return JSONResponse({"ok": False, "error": "标的不在持仓/关注列表中"}, 404)
        amount = body.get("amount_cny") or body.get("amount") or 0
        from .portfolio_routes import portfolio_diagnosis
        try:
            diag = await portfolio_diagnosis()
        except Exception:
            diag = {"rows": [], "totals": {"mv": 0}}
        quote = None
        bars: list[dict] = []
        try:
            from .panel_api import _gtimg
            quote = (await _gtimg.get_quotes([st["gtimg"]])).get(st["gtimg"])
        except Exception:
            pass
        try:
            bars = await asyncio.to_thread(fetch_kline, kline_sym_of(st.get("gtimg", ""), st), 20)
        except Exception:
            pass
        try:
            tv = await theses.theses_view()
            thesis = (tv.get("items") or {}).get(code)
        except Exception:
            thesis = None
        trades = (read_json(DATA_DIR / "trades.json", {}) or {}).get("trades") or []
        plans = (read_json(DATA_DIR / "plans.json", {}) or {}).get("plans") or []
        signal = None
        try:
            signal = await signals_service.get_signal(code)
        except Exception:
            pass
        result = discipline_mod.evaluate({
            "action": str(body.get("action") or "other"), "code": code,
            "amount_cny": amount, "st": st, "diag": diag, "quote": quote, "bars": bars,
            "thesis": thesis, "trades": trades, "plans": plans, "signal": signal,
            "rules": read_rules(), "today": bj_date()})
        return JSONResponse(result)

    @app.get("/api/reviews")
    async def api_reviews() -> JSONResponse:
        from .kline import default_week
        return JSONResponse({"items": review_mod.list_reviews(), "default_week": default_week()["id"]})

    @app.post("/api/review/generate")
    async def api_review_generate(request: Request) -> JSONResponse:
        body = await _body(request)
        if "__error__" in body:
            return JSONResponse({"ok": False, "error": body["__error__"][1]}, body["__error__"][0])
        wid = body.get("id")
        if wid is not None and not _WEEK_RE.fullmatch(str(wid)):
            return JSONResponse({"ok": False, "error": "bad_id"}, 400)
        try:
            r = await review_mod.build_review(str(wid) if wid else None,
                                              {"notify": body.get("notify") is not False,
                                               "comment": body.get("comment")})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:300]}, 502)
        return JSONResponse(r, 200 if r.get("ok") else 400)

    @app.get("/api/accuracy")
    async def api_accuracy(force: str = "") -> JSONResponse:
        try:
            return JSONResponse(await accuracy_mod.view(force == "1"))
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:200]}, 502)

    @app.get("/api/notify/status")
    async def api_notify_status() -> JSONResponse:
        return JSONResponse(notify_mod.status(_watch_status()))

    @app.post("/api/notify/config")
    async def api_notify_config(request: Request) -> JSONResponse:
        body = await _body(request)
        if "__error__" in body:
            return JSONResponse({"ok": False, "error": body["__error__"][1]}, body["__error__"][0])
        return JSONResponse(notify_mod.update_config(body))

    @app.post("/api/notify/test")
    async def api_notify_test(request: Request) -> JSONResponse:
        if not _local_origin(request):
            return JSONResponse({"ok": False, "error": "forbidden_origin"}, 403)
        r = await notify_mod.notify({"title": "投资Buddy 测试推送", "text": "推送通道正常。",
                                     "level": "info", "source": "test", "force": True})
        return JSONResponse(r)

