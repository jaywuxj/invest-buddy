"""落盘层：原子写入、last-good 保留、成本/运行日志。

铁律：任何 job 只有在拿到「通过质量护栏的新数据」时才写目标文件；
失败路径一律不写，自然保留 last-good。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .settings import DATA_DIR, LOGS_DIR


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # 同分区原子替换


def append_jsonl(path: Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_cost(record: dict) -> None:
    append_jsonl(LOGS_DIR / "cost_log.jsonl", {"ts": time.time(), **record})


def log_run(job: str, status: str, detail: str = "", **extra: Any) -> None:
    append_jsonl(
        LOGS_DIR / "run_log.jsonl",
        {"ts": time.time(), "job": job, "status": status, "detail": detail[:500], **extra},
    )


def data_path(name: str) -> Path:
    return DATA_DIR / name
