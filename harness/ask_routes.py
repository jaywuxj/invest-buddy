"""右侧问答 API：注册到现有 FastAPI 应用，不依赖旧面板服务。"""
from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import ask_service
from .app_settings import model_for

MAX_BODY_BYTES = 64 * 1024
MAX_MESSAGES = 20
MAX_MESSAGE_CHARS = 6000


def _code(raw: object) -> str:
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(400, "代码不合法")
    code = raw or "portfolio"
    if not ask_service.CODE_RE.fullmatch(code):
        raise HTTPException(400, "代码不合法")
    return code


def register_ask_routes(app: FastAPI) -> None:
    @app.get("/api/ask/suggest")
    async def ask_suggest(code: str = "portfolio") -> dict:
        try:
            return ask_service.suggest_for(_code(code))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/ask/context")
    async def ask_context(code: str = "portfolio") -> dict:
        try:
            return ask_service.context_for(_code(code))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/ask")
    async def ask(request: Request) -> StreamingResponse:
        if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > MAX_BODY_BYTES:
            raise HTTPException(413, "请求体过大")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BODY_BYTES:
                raise HTTPException(413, "请求体过大")
            raw.extend(chunk)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, "请求体须为 JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "请求体须为 JSON 对象")
        code = _code(body.get("code"))
        mode, web = body.get("mode", "deep"), body.get("web", "off")
        if web == "on":  # 8899 前端契约：on → auto（独立版强制不联网，服务层会如实降级提示）
            web = "auto"
        if mode not in ("fast", "deep", "auto") or web not in ("off", "auto"):
            raise HTTPException(400, "mode 须为 fast|deep|auto，web 须为 off|auto")
        if body.get("attachments"):
            raise HTTPException(400, "独立后端暂不支持附件，请使用文字提问")
        messages = body.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
            raise HTTPException(400, "messages 须为 1–20 条")
        if any(not isinstance(m, dict) or m.get("role") not in ("user", "assistant")
               or not isinstance(m.get("content"), str) or not m["content"].strip()
               or len(m["content"]) > MAX_MESSAGE_CHARS for m in messages):
            raise HTTPException(400, "对话角色或文本不合法（每条最多 6000 字）")
        if messages[-1]["role"] != "user":
            raise HTTPException(400, "最后一条消息必须是用户问题")
        try:
            ask_service.context_for(code)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

        async def events():
            try:
                async with asyncio.timeout(max(180, model_for("ask")["timeout_ms"] / 1000 + 30)):
                    async for item in ask_service.ask_events(code, messages, mode, web):
                        yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
            except TimeoutError:
                yield "data: " + json.dumps({"error": "问答超时，请重试或缩小问题范围"}, ensure_ascii=False) + "\n\n"
            except Exception as exc:
                # 不暴露原始第三方响应与绝对路径；URL/API key 永不回传客户端。
                detail = str(exc)
                if "模型 HTTP" in detail:
                    detail = detail.split("：", 1)[0]
                elif not isinstance(exc, RuntimeError):
                    detail = "问答服务暂不可用"
                yield "data: " + json.dumps({"error": detail[:200]}, ensure_ascii=False) + "\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff",
        })
