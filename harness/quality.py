"""质量护栏：JSON 抽取、Schema 校验、数字回读对账（防幻觉核心，纯程序零成本）。"""
from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft7Validator

from .settings import RECONCILE_MIN_MAGNITUDE, RECONCILE_TOLERANCE


def extract_json(text: str) -> dict:
    """从 LLM 输出中抽取第一个完整 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start = t.find("{")
    if start < 0:
        raise ValueError("输出中未找到 JSON 对象")
    depth = 0
    for i in range(start, len(t)):
        c = t[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start : i + 1])
    raise ValueError("JSON 括号不闭合")


def validate_schema(obj: Any, schema: dict) -> list[str]:
    v = Draft7Validator(schema)
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in v.iter_errors(obj)]


# 注意：前向断言只排除 ASCII 数字/点/逗号——Python 的 \w 会匹配中文，「净利1,141.15」会被切成 141.15（踩过的坑）
_TEXT_NUM_RE = re.compile(r"(?<![0-9.,])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![0-9.,])-?\d+(?:\.\d+)?")


def numbers_in_text(s: str) -> list[float]:
    """抽取字符串中的数字（兼容千分位逗号）。日期 2026-09-28 会拆成 2026/09/28，年份与小数由对账豁免。"""
    out = []
    for m in _TEXT_NUM_RE.finditer(s or ""):
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return out


def collect_numbers(obj: Any, path: str = "", text: bool = False) -> list[tuple[str, float]]:
    """遍历 JSON 收集所有数值叶子（跳过布尔）；text=True 时连字符串内的数字一并收集。"""
    out: list[tuple[str, float]] = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.append((path, float(obj)))
    elif isinstance(obj, str):
        if text:
            out.extend((path, v) for v in numbers_in_text(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(collect_numbers(v, f"{path}.{k}", text))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(collect_numbers(v, f"{path}[{i}]", text))
    return out


_YEAR_RE = re.compile(r"^(19|20)\d{2}(\.\d+)?$")


def reconcile_numbers(
    output_obj: Any,
    source_numbers: list[float],
    tol: float = RECONCILE_TOLERANCE,
    min_magnitude: float = RECONCILE_MIN_MAGNITUDE,
    text: bool = False,
) -> list[dict]:
    """数字回读对账：输出中的大数值必须能在注入数据中找到近似来源（±tol）。

    豁免：年份、|v| < min_magnitude 的小数（分数/比率/序号类）。
    text=True：叙述性字段里的数字也参与对账（onepager/信号摘要等文本产出）。
    返回可疑数字清单；空列表 = 通过。
    """
    if not source_numbers:
        return []
    suspicious = []
    for path, v in collect_numbers(output_obj, text=text):
        if _YEAR_RE.match(str(v)):
            continue
        if abs(v) < min_magnitude:
            continue
        ok = any(
            s != 0 and abs(v - s) / max(abs(s), 1e-9) <= tol
            for s in source_numbers
        )
        if not ok:
            suspicious.append({"path": path, "value": v})
    return suspicious


def numbers_from_digest(digest: Any) -> list[float]:
    """从注入给模型的数据摘要中提取全部基准数字（含字符串内数字，如新闻里的回购金额）。"""
    return [v for _, v in collect_numbers(digest, text=True)]
