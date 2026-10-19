"""Skill 注册表：扫描 skills/ 目录，SKILL.md(frontmatter) + prompt.j2 + schema.json，mtime 热更新。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Template

from .settings import SKILLS_DIR


@dataclass
class Skill:
    name: str
    dir: Path
    meta: dict[str, Any] = field(default_factory=dict)
    prompt_template: str = ""
    schema: dict = field(default_factory=dict)
    mtime: float = 0.0

    @property
    def model_route(self) -> str:
        return str(self.meta.get("model", "flash"))

    @property
    def quality(self) -> str:
        return str(self.meta.get("quality", "standard"))  # standard=校验+回读；deep=再加双模型核对

    @property
    def max_tokens(self) -> int:
        return int(self.meta.get("max_tokens", 4000))

    def render_prompt(self, context: dict) -> str:
        return Template(self.prompt_template).render(**context)


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """极简 frontmatter 解析（key: value 行），避免强依赖 YAML 结构。"""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, text[m.end():]


class SkillRegistry:
    def __init__(self, root: Path = SKILLS_DIR) -> None:
        self.root = root
        self._cache: dict[str, Skill] = {}

    def _dir_mtime(self, d: Path) -> float:
        mt = 0.0
        for f in d.glob("*"):
            if f.is_file():
                mt = max(mt, f.stat().st_mtime)
        return mt

    def get(self, name: str) -> Skill:
        d = self.root / name
        if not d.is_dir():
            raise KeyError(f"skill 不存在: {name}（{d}）")
        mt = self._dir_mtime(d)
        cached = self._cache.get(name)
        if cached and cached.mtime >= mt:
            return cached
        skill_md = d / "SKILL.md"
        prompt_f = d / "prompt.j2"
        schema_f = d / "schema.json"
        if not skill_md.exists() or not prompt_f.exists():
            raise KeyError(f"skill {name} 缺少 SKILL.md 或 prompt.j2")
        meta, body = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        meta.setdefault("description", body.strip().splitlines()[0] if body.strip() else "")
        schema = {}
        if schema_f.exists():
            schema = json.loads(schema_f.read_text(encoding="utf-8"))
        skill = Skill(
            name=name,
            dir=d,
            meta=meta,
            prompt_template=prompt_f.read_text(encoding="utf-8"),
            schema=schema,
            mtime=mt,
        )
        self._cache[name] = skill
        return skill

    def list(self) -> list[dict]:
        out = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.name == "personas":  # personas/ 为大师人设资产，不是任务 skill
                continue
            try:
                s = self.get(d.name)
                out.append({
                    "name": s.name,
                    "model": s.model_route,
                    "quality": s.quality,
                    "description": s.meta.get("description", ""),
                    "has_schema": bool(s.schema),
                    "mtime": s.mtime,
                })
            except Exception as e:
                out.append({"name": d.name, "error": str(e)})
        return out

    def reload(self) -> None:
        self._cache.clear()


registry = SkillRegistry()
