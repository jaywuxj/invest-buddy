"""Skill 化任务流水线：渲染 prompt → LLM（重试降温）→ JSON 抽取 → Schema 校验
→ 数字回读对账 →（deep 级）双模型核对 → 原子落盘（pending_review 人审位）。

失败铁律：任何一步失败都不写目标文件，保留 last-good，写 run_log。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import llm, quality
from ..events import bus
from ..settings import LLM_MAX_RETRIES
from ..skills import Skill
from ..store import atomic_write_json, log_run


class JobFailed(Exception):
    pass


async def run_skill_pipeline(
    skill: Skill,
    context: dict,
    output_file: Path,
    *,
    source_numbers: list[float] | None = None,
    extra_meta: dict | None = None,
) -> dict:
    """返回写盘后的完整对象（含 _meta）。失败抛 JobFailed。"""
    t0 = time.time()
    user_prompt = skill.render_prompt(context)
    system = (
        "你是严谨的投资分析助手。只使用用户消息中提供的数据，绝不编造未提供的数字；"
        "数据缺失时写 null 并说明。严格按提示词要求输出 JSON，不要输出任何其他文字。"
    )

    last_err = ""
    result: dict | None = None
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            resp = await llm.chat(
                skill.model_route,
                system,
                user_prompt,
                temperature=max(0.0, 0.3 - attempt * 0.15),
                max_tokens=skill.max_tokens,
                skill=skill.name,
            )
            result = quality.extract_json(resp["text"])
            break
        except Exception as e:
            last_err = f"attempt{attempt}: {e}"
            # 调试留证：失败时把原始响应写入 logs/last_failure_<skill>.txt
            try:
                from ..settings import LOGS_DIR
                raw = resp["text"] if "resp" in dir() else ""  # noqa: F821
                (LOGS_DIR / f"last_failure_{skill.name}.txt").write_text(
                    f"error: {last_err}\n\n--- raw response ---\n{raw[:4000]}",
                    encoding="utf-8",
                )
            except Exception:
                pass
    if result is None:
        raise JobFailed(f"LLM/JSON 抽取失败: {last_err}")

    # 防线 2：Schema 校验
    if skill.schema:
        errors = quality.validate_schema(result, skill.schema)
        if errors:
            raise JobFailed(f"schema 校验失败: {'; '.join(errors[:5])}")

    # 防线 3：数字回读对账
    suspicious: list[dict] = []
    if source_numbers:
        text_mode = str(skill.meta.get("reconcile_text", "")).lower() == "true"
        suspicious = quality.reconcile_numbers(result, source_numbers, text=text_mode)
        if len(suspicious) > max(3, len(source_numbers) // 20):
            raise JobFailed(f"数字回读对账失败（{len(suspicious)} 个数字无法对账）: {suspicious[:5]}")

    # 防线 4（deep 级）：双模型核对——checker 判断结论是否与数据矛盾
    review: dict | None = None
    if skill.quality == "deep":
        try:
            check_prompt = (
                "以下是注入给分析模型的原始数据与模型输出结论。请判断结论是否与数据明显矛盾"
                "（数字引用错误、方向判断与数据相反）。只输出 JSON："
                '{"consistent": true/false, "issues": ["..."]}\n\n'
                f"## 原始数据\n{_clip(context)}\n\n## 模型输出\n{_clip(result)}"
            )
            r = await llm.chat("checker", "你是事实核对员，只输出 JSON。", check_prompt,
                               temperature=0.0, max_tokens=800, skill=skill.name + ":check")
            review = quality.extract_json(r["text"])
        except Exception as e:
            review = {"consistent": None, "issues": [f"checker 不可用: {e}"]}

    # 防线 5：人审状态位
    result.setdefault("_meta", {})
    result["_meta"].update({
        "skill": skill.name,
        "model_route": skill.model_route,
        "status": "pending_review",
        "suspicious_numbers": suspicious,
        "checker": review,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_ms": int((time.time() - t0) * 1000),
        **(extra_meta or {}),
    })
    atomic_write_json(output_file, result)
    bus.publish("job_done", {"job": skill.name, "file": str(output_file)})
    log_run(skill.name, "ok", f"写盘 {output_file.name}", ms=result["_meta"]["elapsed_ms"])
    return result


def _clip(obj: Any, limit: int = 6000) -> str:
    import json

    s = json.dumps(obj, ensure_ascii=False)
    return s[:limit]
