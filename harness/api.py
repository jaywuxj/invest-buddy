"""开放 API 层：任何客户端随时触发任务 / 深研 / 订阅 SSE。这正是脱离 WorkBuddy 的核心收益。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import llm, portfolio
from .events import bus
from .jobs import job_list, run_job
from .panel_api import panel_quotes, register_panel_routes
from .portfolio_routes import register_portfolio_routes
from .settings_routes import register_settings_routes
from .ask_routes import register_ask_routes
from .research_routes import register_research_routes
from .decision_routes import register_decision_routes
from .quotehub import latest_quotes, quotehub_status, start_quotehub
from .scheduler import guarded_run, schedule_list, start_scheduler
from .settings import DATA_DIR, LOGS_DIR
from .skills import registry
from .store import read_json

app = FastAPI(title="invest-harness", version="0.1.0")

# 只暴露明确的 harness 任务产物；用户配置和密钥不走通用读文件端点。
_PUBLIC_DATA_FILES = frozenset({"preopen", "events", "signals", "onepager"})
_DILIGENCE_CODE = re.compile(r"[A-Za-z0-9]{1,20}\.(?:HK|US|SH|SZ|BJ)\Z")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})


def _same_local_origin(url: str, request: Request, *, origin: bool = False) -> bool:
    try:
        ref = urlsplit(url)
        target = urlsplit(str(request.url))
        if ref.scheme not in ("http", "https") or ref.hostname not in _LOCAL_HOSTS:
            return False
        if target.hostname not in _LOCAL_HOSTS or ref.hostname != target.hostname:
            return False
        if ref.username or ref.password or (origin and (ref.path or ref.query or ref.fragment)):
            return False
        ref_port = ref.port if ref.port is not None else (443 if ref.scheme == "https" else 80)
        target_port = target.port if target.port is not None else (443 if target.scheme == "https" else 80)
        return ref.scheme == target.scheme and ref_port == target_port
    except ValueError:
        return False


@app.middleware("http")
async def reject_cross_site_writes(request: Request, call_next):
    host_header = request.headers.get("host", "")
    try:
        host = urlsplit("http://" + host_header).hostname
    except ValueError:
        host = None
    if host not in _LOCAL_HOSTS:
        return JSONResponse({"ok": False, "error": "forbidden_host"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        headers = request.headers
        fetch_site = headers.get("sec-fetch-site", "").lower()
        origin, referer = headers.get("origin"), headers.get("referer")
        if (fetch_site in ("cross-site", "same-site")
                or (origin is not None and not _same_local_origin(origin, request, origin=True))
                or (referer is not None and not _same_local_origin(referer, request))):
            return JSONResponse({"ok": False, "error": "forbidden_origin"}, status_code=403)
    return await call_next(request)


@app.on_event("startup")
async def _startup() -> None:
    start_scheduler()
    await start_quotehub()


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "llm_routes": llm.route_status(),
        "quotehub": quotehub_status(),
        "schedules": schedule_list(),
        "config_loaded": bool(portfolio.all_stocks()),
        "panel_data": panel_data_status(),
    }


def panel_data_status() -> dict:
    """各面板契约文件的生成时间（全部来自 harness 自产目录）。"""
    from .settings import PANEL_OUT_DIR
    from .store import read_json

    out = {}
    for n in ("fundamentals", "earnings", "research", "events", "analysis", "themes", "trends", "onepager"):
        d = read_json(PANEL_OUT_DIR / f"{n}.json", None)
        out[n] = (d or {}).get("generated_at") if isinstance(d, dict) else None
    return out


@app.get("/api/jobs")
async def jobs() -> dict:
    return {"jobs": job_list(), "schedules": schedule_list()}


@app.post("/api/jobs/{name}")
async def trigger_job(name: str) -> dict:
    """随时触发任务：异步执行，立即返回受理结果。"""
    known = {j["name"] for j in job_list()}
    if name not in known or name == "deep-diligence":
        raise HTTPException(404, f"未知或需参数的任务: {name}（深研用 /api/analyze/<code>）")
    asyncio.create_task(guarded_run(name, "api"))
    return {"ok": True, "job": name, "accepted": True}


@app.post("/api/analyze/{code}")
async def analyze(code: str) -> dict:
    """随时对单只股票跑深研全流程。同步等待结果（30-90s），也可轮询 data/diligence/<code>.json。"""
    _check_diligence_code(code)
    if not portfolio.stock_meta(code):
        raise HTTPException(404, f"标的 {code} 不在 holdings.json")
    asyncio.create_task(guarded_run("deep-diligence", "api", code=code))
    return {"ok": True, "code": code, "accepted": True, "hint": f"GET /api/data/diligence/{code} 查看结果"}


@app.get("/api/quotes")
async def quotes(codes: str = "") -> dict:
    """带 codes 参数时为 panel 契约的 gtimg 代理（前端用）；否则返回 quotehub 最新快照。"""
    if codes:
        return await panel_quotes(codes)
    return {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "quotes": latest_quotes()}


@app.get("/api/skills")
async def skills() -> dict:
    return {"skills": registry.list()}


@app.post("/api/skills/reload")
async def skills_reload() -> dict:
    registry.reload()
    return {"ok": True, "skills": [s["name"] for s in registry.list()]}


def _check_diligence_code(code: str) -> None:
    if not _DILIGENCE_CODE.fullmatch(code):
        raise HTTPException(400, "bad code")


def _diligence_path(code: str):
    _check_diligence_code(code)
    return DATA_DIR / "diligence" / f"{code}.json"


@app.get("/api/data/{name}")
async def data_file(name: str) -> JSONResponse:
    """仅读取白名单任务产物，diligence 子目录另走代码校验。"""
    if name not in _PUBLIC_DATA_FILES:
        raise HTTPException(404, "unknown data")
    f = DATA_DIR / f"{name}.json"
    if not f.exists():
        return JSONResponse(None)
    if f.is_symlink() or not f.is_file():
        raise HTTPException(404, "unknown data")
    return JSONResponse(read_json(f))


@app.get("/api/data/diligence/{code}")
async def diligence_file(code: str) -> JSONResponse:
    f = _diligence_path(code)
    if not f.exists():
        return JSONResponse(None)
    if f.parent.is_symlink() or f.is_symlink() or not f.is_file():
        raise HTTPException(404, "无该标的深研结果")
    return JSONResponse(read_json(f))


class ReviewBody(BaseModel):
    action: str  # approve / reject
    comment: str = ""


@app.post("/api/data/diligence/{code}/review")
async def review_diligence(code: str, body: ReviewBody) -> dict:
    """人审闭环：确认/驳回。驳回样本回流 golden（后续接入回归库）。"""
    f = _diligence_path(code)
    if f.parent.is_symlink() or f.is_symlink() or not f.is_file():
        raise HTTPException(404, "无该标的深研结果")
    obj = read_json(f)
    if not (isinstance(obj, dict) and isinstance(obj.get("_meta"), dict)
            and obj["_meta"].get("code") == code
            and obj["_meta"].get("skill") == "deep-diligence"):
        raise HTTPException(404, "无该标的深研结果")
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action 须为 approve|reject")
    obj["_meta"]["status"] = "approved" if body.action == "approve" else "rejected"
    obj["_meta"]["review_comment"] = body.comment[:500]
    from .store import atomic_write_json
    atomic_write_json(f, obj)
    return {"ok": True, "code": code, "status": obj["_meta"]["status"]}


@app.get("/api/costs")
async def costs(days: int = 30) -> dict:
    """成本看板：按天/按 skill 聚合 cost_log。"""
    f = LOGS_DIR / "cost_log.jsonl"
    total, by_skill, by_model, n = 0.0, {}, {}, 0
    cutoff = time.time() - days * 86400
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ts", 0) < cutoff:
                continue
            n += 1
            c = r.get("cost_yuan", 0)
            total += c
            by_skill[r.get("skill", "?")] = round(by_skill.get(r.get("skill", "?"), 0) + c, 6)
            by_model[r.get("model", "?")] = round(by_model.get(r.get("model", "?"), 0) + c, 6)
    return {"days": days, "calls": n, "total_yuan": round(total, 4), "by_skill": by_skill, "by_model": by_model}


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    """SSE：quote / alert / job_start / job_done / job_error 实时推送。"""
    q = bus.subscribe()

    async def gen():
        try:
            yield 'event: hello\ndata: {"ok": true}\n\n'
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # 保活
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# 面板 v2 兼容层（/、/api/holdings、/api/notes、/api/expert 等 panel 契约端点）
register_panel_routes(app)
register_portfolio_routes(app)
register_settings_routes(app)
register_ask_routes(app)
register_research_routes(app)
register_decision_routes(app)
from .panel_plus_routes import register_plus_routes
register_plus_routes(app)
