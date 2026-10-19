"""westock CLI 接入（腾讯自选股研报库 / 机构评级，本地二进制，免费免登录）。

- 二进制路径：环境变量 WESTOCK_BIN 覆盖，默认 ~/.local/bin/westock；缺失时 available()=False，
  所有调用方按「未接入」如实降级，不报错、不编造；
- 本机限频经验值约 25 只/分钟，批量调用务必串行并限速（由 research_reports 服务层控制）；
- 研报全文磁盘缓存 data/cache/reports/<id>.md（8899 同口径）。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from .. import settings

_LOCK = threading.Lock()
_MIN_INTERVAL_SEC = 2.5  # 限频保护：CLI 调用最小间隔
_last_call = 0.0

REPORT_TIMEOUT_SEC = float(os.environ.get("REPORT_TIMEOUT_SEC", "30"))
REPORT_CACHE_DIR_NAME = "cache/reports"


def bin_path() -> str:
    return os.environ.get("WESTOCK_BIN") or str(Path.home() / ".local" / "bin" / "westock")


def available() -> bool:
    p = bin_path()
    return os.path.isfile(p) and os.access(p, os.X_OK)


def run(args: list[str], timeout: float | None = None) -> str:
    """执行 CLI 并返回 stdout；不可用/超时/失败抛 RuntimeError。"""
    if not available():
        raise RuntimeError("westock_unavailable")
    global _last_call
    with _LOCK:
        import time
        wait = _MIN_INTERVAL_SEC - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
    try:
        cp = subprocess.run([bin_path(), *args], capture_output=True, text=True,
                            timeout=timeout or REPORT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        raise RuntimeError("timeout")
    except OSError as exc:
        raise RuntimeError(f"spawn_failed:{exc}") from exc
    if cp.returncode != 0:
        raise RuntimeError(f"exit_{cp.returncode}:{(cp.stderr or '')[:120]}")
    return cp.stdout


def num(v: Any) -> float | None:
    try:
        f = float(str(v).replace(",", "").replace("%", ""))
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def tables(md: str) -> list[dict]:
    """解析 CLI 的 markdown 输出：#### 段标题 + 表格 → [{section, title, rows}]。"""
    out: list[dict] = []
    section = ""
    lines = (md or "").split("\n")
    cur_title: str | None = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("####"):
            section = ln.lstrip("#").strip()
            cur_title = None
        elif ln.startswith("**") and ln.rstrip().endswith("**"):
            cur_title = ln.strip("*").strip()
        elif ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s\-|]+\|", lines[i + 1] or ""):
            header = [c.strip() for c in ln.strip("|").split("|")]
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                cells = [c.strip() for c in lines[j].strip("|").split("|")]
                rows.append(dict(zip(header, cells)))
                j += 1
            out.append({"section": section, "title": cur_title or "", "rows": rows})
            i = j
            continue
        i += 1
    return out


def report_list(gtimg: str, limit: int = 100, offset: int = 0) -> list[dict]:
    """研报列表 → 标准化条目（移植 8899 research_reports.js parseList）。"""
    md = run(["report", "list", gtimg, "--limit", str(limit), "--offset", str(offset)])
    rows = []
    for t in tables(md):
        if not t["rows"] or "id" not in t["rows"][0]:
            continue
        for o in t["rows"]:
            m = re.match(r"^【([^】]+)】\s*(.*)$", o.get("title") or "")
            org = m[1] if m else ""
            title = (m[2] if m else o.get("title") or "")
            title = re.sub(r"^[^：:（(]{1,20}[（(][0-9A-Z.]{3,12}[）)]\s*[：:]?\s*", "", title)
            rows.append({"report_id": o.get("id"), "date": (o.get("time") or "")[:10],
                         "org": org, "title": title or o.get("title"), "full_title": o.get("title"),
                         "rating": o.get("tzpj") or "",
                         "typeStr": o.get("typeStr") if re.search(r"点评|报告|研究|跟踪", o.get("typeStr") or "") else ""})
    return [r for r in rows if r["report_id"] and r["date"]]


def report_detail(report_id: str) -> dict:
    """研报全文：磁盘缓存优先；返回 {ok, id, text, cached}。"""
    if not re.fullmatch(r"re[a-z]\d{6,20}", report_id or ""):
        return {"ok": False, "error": "bad_id"}
    cache = settings.DATA_DIR / REPORT_CACHE_DIR_NAME / f"{report_id}.md"
    try:
        text = cache.read_text(encoding="utf-8")
        if len(text) > 200:
            return {"ok": True, "id": report_id, "text": text, "cached": True}
    except OSError:
        pass
    out = run(["report", "detail", report_id])
    text = out.strip()
    if len(text) < 100 or re.search(r"失败|error", text[:60], re.I):
        return {"ok": False, "error": "fetch_failed", "detail": text[:200]}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return {"ok": True, "id": report_id, "text": text, "cached": False}


def rating(codes: list[str]) -> dict[str, dict]:
    """机构评级（港股/美股）：{gtimg: {cur, monthly, tp}}（移植 8899 consensus_track.js parseRating）。"""
    if not codes:
        return {}
    md = run(["rating", ",".join(codes)], timeout=45)
    out: dict[str, dict] = {}
    for t in tables(md):
        sec = t["section"]
        if not sec:
            continue
        cur = out.setdefault(sec, {"cur": None, "monthly": [], "tp": []})
        title = t.get("title") or ""
        # 标题匹配要与 8899 同样精确：「目标价 & 当前评级」≠「价格 vs 目标价走势」
        if re.search(r"目标价\s*&", title) and t["rows"]:
            r = t["rows"][0]
            cnt = num(r.get("ratingCnt"))
            buy = (num(r.get("ratingBuyCnt")) or 0) + (num(r.get("ratingIncCnt")) or 0)
            sell = (num(r.get("ratingSellCnt")) or 0) + (num(r.get("ratingDecCnt")) or 0)
            cur["cur"] = {
                "price": num(r.get("currentPrice")), "target_avg": num(r.get("targetPriceAvg")),
                "target_max": num(r.get("targetPriceMax")), "target_min": num(r.get("targetPriceMin")),
                "count": cnt, "institutions": num(r.get("forecastInstitutions")),
                "buy_pct": round(buy / cnt * 100, 2) if cnt else None,
                "sell_pct": round(sell / cnt * 100, 2) if cnt else None,
                "hold_pct": round((num(r.get("ratingHoldCnt")) or 0) / cnt * 100, 2) if cnt else None,
            }
        elif "月度趋势" in title:
            for r in t["rows"]:
                cur["monthly"].append({"month": r.get("month"), "buy": num(r.get("buyIncRatio")),
                                       "hold": num(r.get("holdRatio")), "sell": num(r.get("decSellRatio")),
                                       "count": num(r.get("totalCnt"))})
        elif "目标价走势" in title:
            for r in t["rows"]:
                cur["tp"].append({"month": r.get("month"), "close": num(r.get("closePrice")),
                                  "target": num(r.get("targetPriceAvg"))})
    return out


# ---- 估值引擎用：三大表季度序列 / 周 K / 富字段行情 ----

def finance(ws: str, ftype: str, limit: int) -> list[dict]:
    """westock finance <ws> --type income|balance|cashflow --limit N --fields all → rows"""
    md = run(["finance", ws, "--type", ftype, "--limit", str(limit), "--fields", "all"], timeout=60)
    tb = tables(md)
    return tb[0]["rows"] if tb else []


def kline(ws: str, period: str = "week", limit: int = 262, fq: str = "qfq") -> list[dict]:
    """周/日 K → [{d, c}] 升序。"""
    md = run(["kline", ws, "--period", period, "--limit", str(limit), "--fq", fq], timeout=45)
    tb = tables(md)
    rows = tb[0]["rows"] if tb else []
    out = [{"d": str(r.get("date") or "")[:10], "c": num(r.get("last") if r.get("last") is not None else r.get("close"))}
           for r in rows]
    return sorted([x for x in out if x["d"] and x["c"] and x["c"] > 0], key=lambda x: x["d"])


_quote_mem: dict[str, tuple[float, dict]] = {}


def quote(codes: list[str], force: bool = False) -> dict[str, dict]:
    """富字段行情（pe_ratio/pb_ratio/total_market_cap/dividend_ratio_ttm/chg_ytd…），10 分钟缓存。"""
    import time as _t
    need = [c for c in codes if force or c not in _quote_mem or _t.time() - _quote_mem[c][0] > 600]
    if need:
        md = run(["quote", ",".join(need)], timeout=30)
        for tb in tables(md):
            for r in tb["rows"]:
                if r.get("code"):
                    _quote_mem[r["code"]] = (_t.time(), r)
    return {c: _quote_mem[c][1] for c in codes if c in _quote_mem}
