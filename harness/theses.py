"""投资逻辑卡（移植 8899 server.js 的 theses 子系统，契约完全一致）。

存储：data/config/theses.json {updated_at, items: {code: {...}}}（用户记录）。
评估：thesesView() 为每张卡附加 eval（指标越线 / 证伪命中 / 安全边际 / 复查到期）。
迁移：首次读取时把旧版 data/decisions/theses.json（简版判断）转换为新结构，原文件归档不删。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import portfolio
from .kline import bj_date, bj_full, shift_date
from . import settings
from .store import atomic_write_json, read_json

_TZ = timezone(timedelta(hours=8))
_LOCK = threading.RLock()
_MAX_FILE = 2 * 1024 * 1024
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

THESIS_METRICS = {
    "price": {"label": "股价", "unit": ""},
    "pe": {"label": "市盈率 TTM", "unit": "倍"},
    "pnl_pct": {"label": "持仓浮盈亏", "unit": "%"},
    "upside": {"label": "距机构目标均价空间", "unit": "%"},
    "roe": {"label": "ROE（年化）", "unit": "%"},
    "gross_margin": {"label": "毛利率", "unit": "%"},
    "net_margin": {"label": "净利率", "unit": "%"},
    "rev_yoy": {"label": "营收同比", "unit": "%"},
    "debt_ratio": {"label": "资产负债率", "unit": "%"},
}

def _theses_file() -> Path:
    return settings.CONFIG_DIR / "theses.json"


def _drafts_file() -> Path:
    return settings.CONFIG_DIR / "thesis_drafts.json"


def _legacy_file() -> Path:
    return settings.DATA_DIR / "decisions" / "theses.json"


def _valid_date(s: str) -> bool:
    if not _DATE_RE.match(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _clip(s: Any, n: int) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()[:n]


def _migrate_legacy() -> None:
    """把旧版简版判断（data/decisions/theses.json）升级为 8899 结构；原文件保留不删。"""
    if _theses_file().exists() or not _legacy_file().exists():
        return
    legacy = read_json(_legacy_file(), None)
    rows = (legacy or {}).get("theses")
    if not isinstance(rows, list):
        return
    items: dict[str, Any] = {}
    now = bj_full()
    for row in rows:
        if not isinstance(row, dict) or not row.get("code"):
            continue
        falsifiers = [{"text": _clip(f, 200), "hit": False, "hit_at": None}
                      for f in (row.get("falsifiers") or []) if _clip(f, 200)]
        log = [{"at": _clip(row.get("updated_at"), 19) or now, "text": "由旧版「我的判断」迁移"}]
        if row.get("thesis"):
            log.append({"at": now, "text": "原判断全文：" + _clip(row["thesis"], 400)})
        if row.get("note"):
            log.append({"at": now, "text": "原备注：" + _clip(row["note"], 400)})
        if row.get("metrics"):
            log.append({"at": now, "text": "原跟踪事项（文本）：" + "；".join(_clip(m, 120) for m in row["metrics"][:6])})
        items[row["code"]] = {
            "code": row["code"], "name": _clip(row.get("name"), 80) or row["code"],
            "created_at": _clip(row.get("created_at"), 19) or now,
            "origin": {"source": "legacy", "generated_at": _clip(row.get("updated_at"), 19), "edits": 0, "confirmed_at": now},
            "reasons": [], "falsifiers": falsifiers[:6], "metrics": [],
            "target_price": None, "margin_pct": 30,
            "review_date": row.get("review_date") if _valid_date(str(row.get("review_date") or "")) else None,
            "summary": _clip(row.get("thesis"), 120), "updated_at": _clip(row.get("updated_at"), 19) or now,
            "log": log[:30],
        }
    atomic_write_json(_theses_file(), {"updated_at": now, "items": items})


def _read_strict(path: Path, validate: Any) -> dict:
    """用户记录严格读：文件不存在 → 空；存在但损坏/结构非法 → 抛错（绝不静默覆盖）。"""
    if not path.exists():
        return {"updated_at": None, "items": {}}
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_FILE:
            raise ValueError("存储文件不安全或过大")
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        from fastapi import HTTPException
        raise HTTPException(500, "逻辑卡存储读取失败，已保留原文件") from exc
    if not validate(d):
        from fastapi import HTTPException
        raise HTTPException(500, "逻辑卡存储结构不合法，已保留原文件")
    return d


def read_theses() -> dict:
    with _LOCK:
        _migrate_legacy()
        return _read_strict(_theses_file(), lambda d: isinstance(d, dict) and isinstance(d.get("items"), dict)
                            and all(isinstance(v, dict) and v.get("code") for v in d["items"].values()))


def _write(d: dict) -> None:
    with _LOCK:
        path = _theses_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.parent / ".theses.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError("逻辑卡存储锁不安全")
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                atomic_write_json(path, d)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def thesis_ctx(code: str, st: dict | None, q: dict | None, fund: dict, res: dict) -> dict:
    f = (fund.get("items") or {}).get(code) or {}
    L, A = f.get("latest") or {}, f.get("annual") or {}
    r = (res.get("items") or {}).get(code) or {}

    def pick(*xs):
        for x in xs:
            if x is not None and isinstance(x, (int, float)):
                return float(x)
        return None

    price = q.get("price") if q and q.get("price") is not None else None
    pe = q.get("pe") if q and q.get("pe") and q["pe"] > 0 else None
    return {
        "price": price,
        "pe": pe,
        "pnl_pct": ((price / st["cost"] - 1) * 100) if (price is not None and st and st.get("type") == "holding" and st.get("cost")) else None,
        "upside": ((r["target_avg"] / price - 1) * 100) if (price and r.get("target_avg")) else None,
        "roe": pick(L.get("roe_annualized"), L.get("roe"), A.get("roe_annualized"), A.get("roe")),
        "gross_margin": pick(L.get("gross_margin"), A.get("gross_margin")),
        "net_margin": pick(L.get("net_margin"), A.get("net_margin")),
        "rev_yoy": pick(L.get("rev_yoy"), A.get("rev_yoy")),
        "debt_ratio": pick(A.get("debt_ratio"), L.get("debt_ratio")),
        "asof": {"quote": (q or {}).get("time"), "fin_period": L.get("period") or A.get("period"),
                 "fund_at": fund.get("generated_at")},
    }


def eval_thesis(t: dict, ctx: dict, today: str) -> dict:
    metrics = []
    for m in (t.get("metrics") or []):
        cur = ctx.get(m.get("metric"))
        breach = cur is not None and (cur < m["value"] if m.get("op") == "<" else cur > m["value"])
        metrics.append({"cur": round(float(cur), 2) if cur is not None else None, "breach": breach})
    falsified = sum(1 for f in (t.get("falsifiers") or []) if f.get("hit"))
    breaches = sum(1 for m in metrics if m["breach"])
    review_date = t.get("review_date") or ""
    due = bool(review_date and review_date <= today)
    overdue = bool(review_date and review_date < today)
    upside = buy_below = margin_ok = None
    if t.get("target_price") and ctx.get("price") is not None:
        upside = (t["target_price"] / ctx["price"] - 1) * 100
        buy_below = t["target_price"] * (1 - (t.get("margin_pct") if t.get("margin_pct") is not None else 30) / 100)
        margin_ok = ctx["price"] <= buy_below
    level = "red" if falsified else ("amber" if breaches or overdue else "ok")
    return {"metrics": metrics, "falsified": falsified, "breaches": breaches,
            "review_due": due, "review_overdue": overdue, "price": ctx.get("price"),
            "upside": round(upside, 1) if upside is not None else None,
            "buy_below": round(buy_below, 2) if buy_below is not None else None,
            "margin_ok": margin_ok, "level": level, "asof": ctx.get("asof")}


async def theses_view(quotes: dict | None = None) -> dict:
    """8899 /api/theses 响应：{updated_at, metrics, items: {code: {...+eval}}, drafts, llm}"""
    d = read_theses()
    fund = read_json(settings.PANEL_OUT_DIR / "fundamentals.json", {}) or {}
    res = read_json(settings.PANEL_OUT_DIR / "research.json", {}) or {}
    today = bj_date()
    out = {}
    for code, t in d["items"].items():
        st = portfolio.stock_meta(code)
        q = None
        if quotes and st:
            q = quotes.get(st.get("gtimg") or "")
        out[code] = {**t, "eval": eval_thesis(t, thesis_ctx(code, st, q, fund, res), today)}
    dr = read_json(_drafts_file(), {}) or {}
    from .app_settings import model_for  # 延迟导入避免环
    try:
        has_llm = bool(model_for("draft").get("key"))
    except Exception:
        has_llm = False
    return {"updated_at": d.get("updated_at"), "metrics": THESIS_METRICS, "items": out,
            "drafts": dr.get("items") or {}, "llm": has_llm}


def mutate_theses(body: dict) -> dict:
    """ops: save / falsifier / reviewed / delete（逐行对齐 8899 mutateTheses）。"""
    with _LOCK:
        _migrate_legacy()
        st = portfolio.stock_meta(str(body.get("code") or ""))
        if not st:
            return {"ok": False, "error": "标的不在持仓/关注列表中"}
        d = read_theses()
        now, today = bj_full(), bj_date()
        t = d["items"].get(st["code"])

        def log(txt: str) -> None:
            t["log"] = ([{"at": now, "text": txt}] + (t.get("log") or []))[:30]

        op = body.get("op")
        if op == "note":
            # 笔记式判断（「笔记」Tab 类型=判断）：只写一句话 summary；已有卡的证伪/指标/复查保留不动
            summary = _clip(body.get("summary"), 300)
            if not summary:
                return {"ok": False, "error": "写一句话判断"}
            if t:
                t["summary"] = summary
            else:
                t = {"code": st["code"], "summary": summary, "reasons": [], "falsifiers": [], "metrics": [],
                     "target_price": None, "margin_pct": None, "review_date": None, "log": []}
            log("更新判断（笔记式）")
            t["updated_at"] = now
            d["items"][st["code"]] = t
            d["updated_at"] = now
            _write(d)
            return {"ok": True, "thesis": t}
        if op == "save":
            reasons = [_clip(x, 200) for x in (body.get("reasons") if isinstance(body.get("reasons"), list) else [])]
            reasons = [x for x in reasons if x][:5]
            if not reasons and not _clip(body.get("summary"), 120):
                return {"ok": False, "error": "写一句话判断：为什么拿着它"}
            prev_f = (t or {}).get("falsifiers") or []
            falsifiers = []
            for x in (body.get("falsifiers") if isinstance(body.get("falsifiers"), list) else [])[:6]:
                text = _clip(x.get("text") if isinstance(x, dict) else x, 200)
                if not text:
                    continue
                old = next((p for p in prev_f if p.get("text") == text), None)
                hit = bool(x.get("hit")) if isinstance(x, dict) and x.get("hit") is not None else bool(old and old.get("hit"))
                falsifiers.append({"text": text, "hit": hit,
                                   "hit_at": (old or {}).get("hit_at") if hit and old and old.get("hit") else (now if hit else None)})
            if not falsifiers:
                return {"ok": False, "error": "至少写 1 条「什么情况说明我错了」"}
            metrics = []
            for m in (body.get("metrics") if isinstance(body.get("metrics"), list) else [])[:6]:
                if not m or not m.get("metric"):
                    continue
                if m["metric"] not in THESIS_METRICS:
                    return {"ok": False, "error": "不支持的跟踪指标：" + str(m["metric"])}
                try:
                    v = float(m.get("value"))
                except (TypeError, ValueError):
                    return {"ok": False, "error": THESIS_METRICS[m["metric"]]["label"] + " 阈值需为数字"}
                metrics.append({"metric": m["metric"], "op": ">" if m.get("op") == ">" else "<",
                                "value": v, "label": _clip(m.get("label"), 40) or THESIS_METRICS[m["metric"]]["label"]})
            target = None
            if body.get("target_price") not in (None, ""):
                try:
                    target = float(body["target_price"])
                except (TypeError, ValueError):
                    return {"ok": False, "error": "目标价需为正数"}
                if not 0 < target < 1e7:
                    return {"ok": False, "error": "目标价需为正数"}
            try:
                margin = 30 if body.get("margin_pct") in (None, "") else float(body["margin_pct"])
            except (TypeError, ValueError):
                return {"ok": False, "error": "安全边际需在 0-80% 之间"}
            if not 0 <= margin <= 80:
                return {"ok": False, "error": "安全边际需在 0-80% 之间"}
            review = str(body.get("review_date") or "") or shift_date(today, 90)
            if not _valid_date(review):
                return {"ok": False, "error": "复查日期格式 YYYY-MM-DD"}
            is_new = t is None
            origin = None
            fd = body.get("from_draft")
            if isinstance(fd, dict):
                origin = {"source": fd.get("source") if fd.get("source") in ("llm", "rules") else "rules",
                          "generated_at": _clip(fd.get("generated_at"), 20),
                          "edits": max(0, int(fd.get("edits") or 0)), "confirmed_at": now}
            base = t or {"code": st["code"], "created_at": now}
            t = {**base, **({"origin": origin} if origin else {}),
                 "name": st.get("name") or st["code"], "reasons": reasons, "falsifiers": falsifiers,
                 "metrics": metrics, "target_price": target, "margin_pct": margin,
                 "review_date": review, "summary": _clip(body.get("summary"), 120), "updated_at": now}
            d["items"][st["code"]] = t
            log(("采纳 AI 草稿并人工确认（" + ("LLM" if origin["source"] == "llm" else "规则") + f"草稿，修改 {origin['edits']} 处）")
                if origin else ("创建逻辑卡" if is_new else "更新逻辑卡"))
            if origin:
                discard_draft(st["code"])
        elif not t:
            return {"ok": False, "error": "thesis_not_found"}
        elif op == "falsifier":
            try:
                f = (t.get("falsifiers") or [])[int(body.get("index"))]
            except (TypeError, ValueError, IndexError):
                return {"ok": False, "error": "falsifier_not_found"}
            f["hit"] = bool(body.get("hit"))
            f["hit_at"] = now if f["hit"] else None
            t["updated_at"] = now
            log(("标记证伪命中：" if f["hit"] else "撤销证伪：") + f["text"])
        elif op == "reviewed":
            nxt = str(body.get("next_date") or "") or shift_date(today, 90)
            if not _valid_date(nxt) or nxt <= today:
                return {"ok": False, "error": "下次复查日期需晚于今天"}
            t["reviewed_at"] = now
            t["review_date"] = nxt
            t["updated_at"] = now
            log("完成复查" + ("：" + _clip(body.get("memo"), 120) if body.get("memo") else "") + "，下次 " + nxt)
        elif op == "delete":
            d["items"].pop(st["code"], None)
        else:
            return {"ok": False, "error": "bad_op"}
        d["updated_at"] = now
        _write(d)
        return {"ok": True, "item": d["items"].get(st["code"])}


# ---- 草稿存储（thesis_draft.py 共用） ----
def read_drafts() -> dict:
    d = read_json(_drafts_file(), None)
    return d if isinstance(d, dict) and isinstance(d.get("items"), dict) else {"items": {}}


def write_drafts(d: dict) -> None:
    _drafts_file().parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_drafts_file(), d)


def discard_draft(code: str) -> bool:
    d = read_drafts()
    had = code in d["items"]
    d["items"].pop(code, None)
    if had:
        write_drafts(d)
    return had
