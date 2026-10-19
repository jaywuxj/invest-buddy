"""独立本地设置 API；不包含 WorkBuddy 同步/确认端点。"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import app_settings

_HEADERS = {"Cache-Control": "no-store"}
_MAX_BODY_BYTES = 65536


class _PayloadTooLarge(ValueError):
    pass


def _response(value: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status_code, headers=_HEADERS)


async def _payload(request: Request) -> dict:
    declared = request.headers.get("content-length", "")
    if declared:
        if not declared.isdecimal():
            raise ValueError("Content-Length 不合法")
        if int(declared) > _MAX_BODY_BYTES:
            raise _PayloadTooLarge("设置请求过大")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_BODY_BYTES:
            raise _PayloadTooLarge("设置请求过大")
        raw.extend(chunk)
    try:
        body = json.loads(raw)
    except ValueError:
        raise ValueError("请求须为 JSON 对象") from None
    if not isinstance(body, dict):
        raise ValueError("请求须为 JSON 对象")
    return body


def _local_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    source = urlsplit(origin)
    target = request.url
    return (source.scheme == target.scheme
            and source.hostname == target.hostname
            and source.port == target.port)


def register_settings_routes(app: FastAPI) -> None:
    @app.get("/api/settings")
    async def get_settings() -> JSONResponse:
        return _response(app_settings.public_settings())

    @app.post("/api/settings")
    async def save_settings(request: Request) -> JSONResponse:
        if not _local_origin(request):
            return _response({"ok": False, "error": "forbidden_origin"}, 403)
        try:
            body = await _payload(request)
            section = body.get("section")
            if not isinstance(section, str):
                raise ValueError("缺少设置分组 section")
            # 兼容 panel：secrets 字段直接位于请求顶层；其他分组使用 value 对象。
            if section != "secrets" and set(body) != {"section", "value"}:
                raise ValueError("设置请求需包含 section 和 value，且不得包含其他字段")
            value = ({k: v for k, v in body.items() if k != "section"} if section == "secrets"
                     else body["value"])
            app_settings.set_section(section, value)
        except _PayloadTooLarge as exc:
            return _response({"ok": False, "error": str(exc)}, 413)
        except ValueError as exc:
            return _response({"ok": False, "error": str(exc)}, 400)
        return _response({"ok": True})

    @app.post("/api/settings/reset")
    async def reset_settings(request: Request) -> JSONResponse:
        if not _local_origin(request):
            return _response({"ok": False, "error": "forbidden_origin"}, 403)
        try:
            body = await _payload(request)
            if set(body) != {"section"}:
                raise ValueError("重置请求只允许 section 字段")
            section = body["section"]
            if not isinstance(section, str):
                raise ValueError("缺少设置分组 section")
            app_settings.reset_section(section)
        except _PayloadTooLarge as exc:
            return _response({"ok": False, "error": str(exc)}, 413)
        except ValueError as exc:
            return _response({"ok": False, "error": str(exc)}, 400)
        return _response({"ok": True})

    @app.post("/api/settings/test-model")
    async def test_model(request: Request) -> JSONResponse:
        if not _local_origin(request):
            return _response({"ok": False, "error": "forbidden_origin"}, 403)
        try:
            body = await _payload(request)
            if set(body) - {"scene"}:
                raise ValueError("模型测试请求仅允许 scene 字段")
            result = app_settings.test_model(body.get("scene", "ask"))
        except _PayloadTooLarge as exc:
            return _response({"ok": False, "error": str(exc)}, 413)
        except ValueError as exc:
            return _response({"ok": False, "error": str(exc)}, 400)
        return _response(result, 200 if result["ok"] else 400)

    @app.get("/api/settings/export")
    async def export_settings() -> JSONResponse:
        return _response(app_settings.export_settings())
