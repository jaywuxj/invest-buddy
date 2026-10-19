"""面板 v2 兼容层：复刻 panel/server.js 的 API 契约，数据 100% 由 harness 自产。

数据归属（完全自给，不读 panel/data、不读 ~/.workbuddy）：
- 用户配置 data/config/：holdings / alerts（预警规则）/ personas（人设展示）
- 契约数据 data/panel/：analysis / fundamentals / research / events / onepager /
  themes / trends / earnings —— 由 jobs/panel_build.py 生产（AKShare + gtimg + DeepSeek）
- harness 自有可写：notes / plans / experts / live / alerts_status
- quotes：gtimg 代理（camelCase 契约）；refresh：gtimg 拉全组合写 live.json
- expert 生成：llm flash 路由 + 本地人设 skill（skills/personas/）
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import filings, llm, portfolio
from .providers.gtimg import GtimgQuoteProvider
from .settings import BASE_DIR, CONFIG_DIR, DATA_DIR, PANEL_OUT_DIR, SKILLS_DIR
from .holdings_service import PortfolioError, _strict_json, _write
from .store import atomic_write_json, read_json

WEB_DIR = BASE_DIR / "web"
USER_SKILLS_DIR = SKILLS_DIR / "personas"  # 大师人设 skill 已迁移至 harness 本地，不再依赖 ~/.workbuddy

NOTES_FILE = DATA_DIR / "notes.json"
PLANS_FILE = DATA_DIR / "plans.json"
EXPERTS_FILE = DATA_DIR / "experts.json"
LIVE_FILE = DATA_DIR / "live.json"

NOTE_MAX_TEXT = 4000
NOTE_MAX_PER_STOCK = 200
PLAN_MAX_OPEN = 50
EXPERT_TTL_SEC = 7 * 24 * 3600

# 面板契约数据端点白名单：config 类读 data/config，其余读 data/panel（harness 自产）
CONFIG_FILES = ("holdings", "alerts", "personas")
PANEL_READONLY = (
    "holdings", "analysis", "fundamentals", "research", "events",
    "alerts", "onepager", "themes", "trends", "earnings",
)

_gtimg = GtimgQuoteProvider()
_NOTES_LOCK = asyncio.Lock()
_PLANS_LOCK = asyncio.Lock()
_EXPERTS_LOCK = asyncio.Lock()
_RECORD_THREAD_LOCK = threading.RLock()
_CODE_RE = re.compile(r"^[0-9A-Za-z.]{1,20}$")
_DECIDE_LOCK = asyncio.Lock()
DECIDE_KEEP_DAYS = 30
_DIMS = ("quality", "growth", "valuation", "views")


def _decide_file() -> Path:
    from . import settings
    return settings.PANEL_OUT_DIR / "decide_history.json"


def read_decide_history() -> dict:
    d = read_json(_decide_file(), {}) or {}
    return {"days": d.get("days") if isinstance(d.get("days"), dict) else {}}


def save_decide_snapshot(body: dict) -> dict:
    """每日结论快照（前端算完全组合后提交，同日覆盖，保留 30 天），用于决策页「最近变化」。"""
    date = str(body.get("date") or "")
    items = body.get("items")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not isinstance(items, dict) or len(items) > 200:
        return {"ok": False, "error": "bad snapshot"}
    clean = {}
    for code, it in items.items():
        if not _CODE_RE.fullmatch(str(code)) or not isinstance(it, dict):
            continue
        dims = it.get("dims") if isinstance(it.get("dims"), dict) else {}
        num = lambda v: round(float(v)) if isinstance(v, (int, float)) and -1 <= v <= 101 else None  # noqa: E731
        clean[code] = {"verdict": str(it.get("verdict") or "")[:12], "score": num(it.get("score")),
                       "dims": {k: num(dims.get(k)) for k in _DIMS}}
    hist = read_decide_history()
    days = hist["days"]
    days[date] = clean
    for old in sorted(days)[:-DECIDE_KEEP_DAYS]:
        days.pop(old, None)
    atomic_write_json(_decide_file(), {"updated_at": _now_iso(), "days": days})
    return {"ok": True, "history": {"days": days}}


# ---------------------------------------------------------------- 基础工具

def _read_panel(name: str) -> Any:
    if name == "holdings":
        return portfolio.load_holdings()
    base = CONFIG_DIR if name in CONFIG_FILES else PANEL_OUT_DIR
    return filings.normalize_panel_sources(name, read_json(base / f"{name}.json", None))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime())


def _new_id() -> str:
    import secrets
    return f"{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def _clip(v: Any, n: int) -> str:
    return str(v if v is not None else "")[:n].strip()


def _record_path(path: Path) -> None:
    if DATA_DIR.is_symlink() or path.parent != DATA_DIR or path.parent.resolve() != DATA_DIR.resolve() or path.is_symlink():
        raise HTTPException(500, f"{path.name} 存储路径不安全；已保留原文件")


def _read_record(path: Path, default: dict, valid: Any) -> dict:
    _record_path(path)
    if not path.exists():
        return default
    try:
        data = _strict_json(path, ())
    except PortfolioError as exc:
        raise HTTPException(500, f"{path.name} 读取失败；已保留原文件") from exc
    if not valid(data):
        raise HTTPException(500, f"{path.name} 内容异常；已保留原文件")
    return data


@contextlib.contextmanager
def _record_write_lock(path: Path):
    with _RECORD_THREAD_LOCK:
        _record_path(path)
        lock_path = DATA_DIR / f".{path.stem}.lock"
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise HTTPException(500, f"{path.name} 锁文件不安全；已保留原文件") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise HTTPException(500, f"{path.name} 锁文件不安全；已保留原文件")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write_record(path: Path, data: dict) -> None:
    _record_path(path)
    try:
        _write(path, data)
    except OSError as exc:
        raise HTTPException(500, f"{path.name} 写入失败；已保留原文件") from exc


# ---------------------------------------------------------------- quotes / live

async def panel_quotes(codes: str) -> dict:
    """GET /api/quotes?codes=hk00700,usAAPL —— panel 契约（camelCase）。"""
    if not re.fullmatch(r"[a-zA-Z0-9,]+", codes or ""):
        raise HTTPException(400, "bad codes")
    raw = await _gtimg.get_quotes(codes.split(","))
    out = {
        k: {
            "name": q.get("name", ""),
            "price": q.get("price"),
            "prevClose": q.get("prev_close"),
            "open": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "pct": q.get("pct"),
            "time": q.get("time", ""),
            "pe": q.get("pe"),
            "marketCap": q.get("market_cap"),
        }
        for k, q in raw.items()
    }
    return {"at": _now_iso(), "quotes": out}


async def refresh_live() -> dict:
    """POST /api/refresh —— gtimg 拉全组合写 live.json。"""
    codes = portfolio.gtimg_codes()
    if not codes:
        return {"ok": False, "error": "无持仓标的"}
    raw = await _gtimg.get_quotes(codes)
    g2c = portfolio.gtimg_to_code()
    quotes = {}
    for gkey, q in raw.items():
        code = g2c.get(gkey)
        if not code:
            continue
        quotes[code] = {
            "name": q.get("name", ""),
            "price": q.get("price"),
            "pct": q.get("pct"),
            "time": q.get("time", ""),
        }
    live = {"fetched_at": _now_iso(), "source": "gtimg(harness)", "quotes": quotes}
    atomic_write_json(LIVE_FILE, live)
    return {"ok": True, "count": len(quotes), "fetched_at": live["fetched_at"]}


def live_status() -> dict:
    live = read_json(LIVE_FILE, None)
    age = None
    if live and live.get("fetched_at"):
        try:
            ts = time.mktime(time.strptime(live["fetched_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            age = int((time.time() - ts) / 60)
        except Exception:
            age = None
    return {
        "token": {"valid": True, "age_min": 0, "note": "harness gtimg 通道，免 token"},
        "live": (
            {
                "fetched_at": live.get("fetched_at"),
                "age_min": age,
                "count": len(live.get("quotes") or {}),
            }
            if live
            else None
        ),
    }


# ---------------------------------------------------------------- notes / plans（harness 自有，语义与 panel/server.js 一致）

def _read_notes() -> dict:
    return _read_record(NOTES_FILE, {}, lambda d: all(
        isinstance(rows, list) and all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows)
        for rows in d.values()
    ))


def mutate_notes(body: dict) -> dict:
    with _record_write_lock(NOTES_FILE):
        return _mutate_notes_locked(body)


def _mutate_notes_locked(body: dict) -> dict:
    code = str(body.get("code") or "")
    if not _CODE_RE.fullmatch(code):
        return {"ok": False, "error": "bad code"}
    op = body.get("op")
    notes = _read_notes()
    lst = notes.get(code) if isinstance(notes.get(code), list) else []
    now = _now_iso()
    if op == "add":
        text = str(body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "empty text"}
        if len(text) > NOTE_MAX_TEXT:
            return {"ok": False, "error": "text too long"}
        if len(lst) >= NOTE_MAX_PER_STOCK:
            return {"ok": False, "error": "too many notes"}
        ntype = _clip(body.get("type") or "note", 10)
        if ntype not in ("note", "thesis", "plan"):
            return {"ok": False, "error": "bad type"}
        note = {
            "id": _new_id(),
            "text": text,
            "type": ntype,
            "source": _clip(body.get("source") or "手动", 20),
            "sourceTab": _clip(body.get("sourceTab"), 20),
            "created_at": now,
            "updated_at": now,
        }
        lst.insert(0, note)
        notes[code] = lst
        _write_record(NOTES_FILE, notes)
        return {"ok": True, "note": note, "notes": lst}
    nid = str(body.get("id") or "")
    idx = next((i for i, n in enumerate(lst) if n and n.get("id") == nid), -1)
    if op == "update":
        text = str(body.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "empty text"}
        if len(text) > NOTE_MAX_TEXT:
            return {"ok": False, "error": "text too long"}
        if idx < 0:
            return {"ok": False, "error": "not found"}
        lst[idx]["text"] = text
        lst[idx]["updated_at"] = now
        _write_record(NOTES_FILE, notes)
        return {"ok": True, "note": lst[idx], "notes": lst}
    if op == "delete":
        if idx < 0:
            return {"ok": False, "error": "not found"}
        lst.pop(idx)
        notes[code] = lst
        _write_record(NOTES_FILE, notes)
        return {"ok": True, "notes": lst}
    return {"ok": False, "error": "bad op"}


def _read_plans() -> list:
    d = _read_record(PLANS_FILE, {"plans": []}, lambda d: isinstance(d.get("plans"), list) and all(
        isinstance(row, dict) and isinstance(row.get("id"), str) for row in d["plans"]
    ))
    return d["plans"]


def _write_plans(plans: list) -> None:
    _write_record(PLANS_FILE, {"updated_at": _now_iso()[:10], "plans": plans})


def mutate_plans(body: dict) -> dict:
    with _record_write_lock(PLANS_FILE):
        return _mutate_plans_locked(body)


def _mutate_plans_locked(body: dict) -> dict:
    op = body.get("op")
    plans = _read_plans()
    now = _now_iso()
    if op == "add":
        code = _clip(body.get("code"), 20)
        if not _CODE_RE.fullmatch(code):
            return {"ok": False, "error": "bad code"}
        title = _clip(body.get("title"), 120)
        if not title:
            return {"ok": False, "error": "empty title"}
        if sum(1 for x in plans if x.get("status") == "open") >= PLAN_MAX_OPEN:
            return {"ok": False, "error": "too many open plans"}
        deadline = _clip(body.get("deadline"), 10)
        if deadline and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
            return {"ok": False, "error": "bad deadline"}
        item = {
            "id": _new_id(),
            "code": code,
            "name": _clip(body.get("name"), 30),
            "title": title,
            "condition": _clip(body.get("condition"), 2000),
            "plan": _clip(body.get("plan"), 2000),
            "deadline": deadline,
            "status": "open",
            "note": _clip(body.get("note"), 2000),
            "result": "",
            "created_at": now,
            "updated_at": now,
            "done_at": None,
        }
        plans.insert(0, item)
        _write_plans(plans)
        return {"ok": True, "plan": item, "plans": plans}
    pid = str(body.get("id") or "")
    idx = next((i for i, x in enumerate(plans) if x.get("id") == pid), -1)
    if op == "update":
        if idx < 0:
            return {"ok": False, "error": "not found"}
        it = plans[idx]
        for k in ("title", "condition", "plan", "note"):
            if body.get(k) is not None:
                it[k] = _clip(body[k], 120 if k == "title" else 2000)
        if body.get("deadline") is not None:
            d = _clip(body["deadline"], 10)
            if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                return {"ok": False, "error": "bad deadline"}
            it["deadline"] = d
        if body.get("status") is not None:
            st = _clip(body["status"], 12)
            if st not in ("open", "done", "cancelled"):
                return {"ok": False, "error": "bad status"}
            # 完成必须写复盘结论
            result = _clip(body["result"] if body.get("result") is not None else it.get("result"), 2000)
            if st == "done" and not result:
                return {"ok": False, "error": "done needs result"}
            if st == "done" and it.get("status") != "done":
                it["done_at"] = now
            if st != "done":
                it["done_at"] = None
            it["status"] = st
        if body.get("result") is not None:
            it["result"] = _clip(body["result"], 2000)
        if not it.get("title"):
            return {"ok": False, "error": "empty title"}
        it["updated_at"] = now
        _write_plans(plans)
        return {"ok": True, "plan": it, "plans": plans}
    if op == "delete":
        if idx < 0:
            return {"ok": False, "error": "not found"}
        plans.pop(idx)
        _write_plans(plans)
        return {"ok": True, "plans": plans}
    return {"ok": False, "error": "bad op"}


# ---------------------------------------------------------------- 专家分析（人设大师 × DeepSeek flash 路由）

# 大师分析 v2（2026-10-15 用户确认）：每位大师只回答自己框架的一个核心问题；不看个人持仓与易变数字。
EXPERT_QUESTIONS = {
    "buffett": "十年后它还能稳定赚钱吗？",
    "munger": "这笔投资会怎么失败？",
    "taleb": "坏情况发生时它扛得住吗？",
    "marks": "市场情绪现在处在什么位置？",
    "duan": "这门生意看得懂、做的是对的事吗？",
    "damodaran": "公司的故事和数字对得上吗？",
}
EXPERT_SCHEMA_HINT = (
    "请严格输出 JSON（不要输出任何其他文字），字段如下：\n"
    '{"stance":"看多|中性|看空","score":-2到2的整数,'
    '"answer":"用一句大白话回答上面的核心问题（30-50 字）",'
    '"worry":"这个框架下最担心的一件事（12-30 字）",'
    '"points":["支撑回答的理由（每条 25-60 字）","…"],'
    '"report":"完整分析（300-600 字，分 2-4 段）"}\n'
    "points 2-3 条。score 含义：+2 强烈看多 / +1 偏多 / 0 中性 / -1 偏空 / -2 强烈看空。\n"
    "写作纪律：只评价公司本身，不知道也不讨论任何人的持仓、买入成本、盈亏或止损线；"
    "不写现价、涨跌幅、市值、PE/PB 等估值倍数和目标价的具体数字（贵不贵用「估值提示」里的定性词）；"
    "财报数字最多引用 1-2 个、只在确有必要时引用——其他大师也会看同一份财报，"
    "你的价值在于用自己框架的独特角度回答问题，不要复述财务数据。不编造名人原话，不用第一人称引语。"
)


def _read_personas() -> list:
    d = _read_panel("personas") or {}
    return d.get("personas") or []


def _load_persona_prompt(skill_name: str) -> str:
    """人设 prompt = SKILL.md + 全部 references（只读用户级 skills 目录）。"""
    d = USER_SKILLS_DIR / skill_name
    if not d.exists():
        raise RuntimeError(f"persona skill not installed: {skill_name}")
    out = (d / "SKILL.md").read_text(encoding="utf-8")
    ref = d / "references"
    if ref.exists():
        for f in sorted(ref.iterdir()):
            if f.suffix == ".md":
                out += f"\n\n---\n# 参考模块 {f.name}\n\n" + f.read_text(encoding="utf-8")
    return out[:24000]


def _build_stock_digest(code: str) -> str:
    """大师分析输入（v2）：只给公司资料——基本面、近几期财报、机构评级倾向、估值定性提示、近期新闻标题。
    刻意不给：个人持仓/成本/股数/盈亏、实时价格、周更 LLM 信号（避免模型引用模型）、目标价等易变数字。"""
    from .jobs.panel_build import _onepager_hints
    holdings = portfolio.load_holdings()
    fund = (_read_panel("fundamentals") or {}).get("items") or {}
    research = (_read_panel("research") or {}).get("items") or {}
    earnings = (_read_panel("earnings") or {}).get("items") or {}
    analysis = _read_panel("analysis") or {}
    meta = next((s for s in (holdings.get("positions") or []) + (holdings.get("watch") or []) if s.get("code") == code), None)
    if not meta:
        raise RuntimeError(f"unknown code: {code}")
    f, r = fund.get(code) or {}, research.get(code) or {}
    up = None
    if r.get("target_avg") and r.get("current_price"):
        up = (r["target_avg"] - r["current_price"]) / r["current_price"] * 100
    hints = _onepager_hints({"valuation": f.get("valuation") or {}, "research": {"upside_pct": up, "rating_latest": r.get("rating_latest")}})
    quarters = [{k: p.get(k) for k in ("label_cn", "revenue", "rev_yoy", "net_profit", "np_yoy", "gross_margin", "ocf") if p.get(k) is not None}
                for p in ((earnings.get(code) or {}).get("periods") or [])[:4]]
    news = [f"- {n.get('title_cn') or n.get('title', '')}（{n.get('time') or n.get('date', '')}）"
            for n in (analysis.get("news") or []) if n.get("code") == code][:6]
    rt = r.get("rating_latest") or {}
    d = {
        "标的": f"{meta.get('name')}（{code}，板块：{meta.get('sector') or '未分类'}）",
        "基本面": json.dumps({"latest": f.get("latest"), "annual": f.get("annual")}, ensure_ascii=False) if f else "暂无",
        "近几期财报": json.dumps(quarters, ensure_ascii=False) if quarters else "暂无",
        "估值提示（定性，不含具体数字）": json.dumps({k: v for k, v in hints.items() if v}, ensure_ascii=False),
        "机构评级倾向": (f"{rt.get('count')} 家机构，买入 {rt.get('buy')}% / 持有 {rt.get('hold')}% / 卖出 {rt.get('sell')}%" if rt else "暂无"),
        "下期一致预期": json.dumps({k: v for k, v in (r.get("next_report") or {}).items() if k in ("period", "np_yoy", "rev_yoy")}, ensure_ascii=False) if r.get("next_report") else "暂无",
        "近期新闻标题": "\n".join(news) if news else "暂无",
    }
    return "".join(f"## {k}\n{v}\n\n" for k, v in d.items())[:8000]


def _parse_expert_json(text: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        raise RuntimeError("LLM 未返回 JSON")
    j = json.loads(m.group(0))
    stance = j.get("stance") if j.get("stance") in ("看多", "中性", "看空") else "中性"
    try:
        score = int(j.get("score"))
    except (TypeError, ValueError):
        score = 1 if stance == "看多" else (-1 if stance == "看空" else 0)
    score = max(-2, min(2, score))
    from .jobs.panel_build import _scrub_private
    points = [_scrub_private(str(x)[:200]) for x in (j.get("points") or [])][:4] if isinstance(j.get("points"), list) else []
    worry = _scrub_private(str(j.get("worry") or j.get("risk") or "")[:200])
    return {
        "stance": stance,
        "score": score,
        "answer": _scrub_private(str(j.get("answer") or "")[:200]),
        "worry": worry,
        "risk": worry,          # 兼容旧字段
        "points": [x for x in points if x],
        "report": _scrub_private(str(j.get("report") or "")[:6000]),
        "v": 2,
    }


def _read_experts() -> dict:
    return _read_record(EXPERTS_FILE, {"updated_at": None, "stocks": {}}, lambda d:
        isinstance(d.get("stocks"), dict) and all(
            isinstance(personas, dict) and all(isinstance(result, dict) for result in personas.values())
            for personas in d["stocks"].values()
        ))


async def generate_expert(code: str, persona_id: str, force: bool) -> dict:
    persona = next((x for x in _read_personas() if x.get("id") == persona_id), None)
    if not persona:
        return {"ok": False, "error": f"unknown persona: {persona_id}"}
    experts = _read_experts()
    cached = (experts["stocks"].get(code) or {}).get(persona_id)
    if not force and cached and cached.get("generated_at"):
        try:
            ts = time.mktime(time.strptime(cached["generated_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            if time.time() - ts < EXPERT_TTL_SEC:
                return {"ok": True, "cached": True, "result": cached}
        except Exception:
            pass
    system = (
        _load_persona_prompt(persona["skill"])
        + "\n\n---\n你正在使用上述思维框架分析一只具体股票。只使用用户提供的数据，不编造未提供的财务数字；"
        "数据不足时给出条件式结论。\n"
        + f"本次你要回答的核心问题：{persona.get('question') or EXPERT_QUESTIONS.get(persona_id, '用你的框架看，这家公司值得长期拥有吗？')}\n"
        + EXPERT_SCHEMA_HINT
    )
    user = "请分析以下标的：\n\n" + _build_stock_digest(code)
    t0 = time.time()
    resp = await llm.chat("flash", system, user, temperature=0.3, max_tokens=4000, skill="expert")
    result = _parse_expert_json(resp["text"])
    result["question"] = persona.get("question") or EXPERT_QUESTIONS.get(persona_id, "")
    result["generated_at"] = _now_iso()
    result["model"] = resp.get("model", "flash")
    result["ms"] = int((time.time() - t0) * 1000)
    # 并发安全：LLM 返回后在锁内「重新读取→合并→写回」。
    # 历史 bug：批量并发生成时各协程持有旧快照互相覆盖，51 条成功只落盘 13 条。
    async with _EXPERTS_LOCK:
        with _record_write_lock(EXPERTS_FILE):
            latest = _read_experts()
            latest["stocks"].setdefault(code, {})[persona_id] = result
            latest["updated_at"] = _now_iso()
            _write_record(EXPERTS_FILE, latest)
    return {"ok": True, "cached": False, "result": result}




# ---------------------------------------------------------------- 路由注册

def register_panel_routes(app: FastAPI) -> None:
    index_file = WEB_DIR / "index.html"

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def panel_home() -> HTMLResponse:
        if not index_file.exists():
            raise HTTPException(404, "web/index.html 不存在")
        return HTMLResponse(
            index_file.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    _STATIC = {
        "p1.css": "text/css", "p1.js": "application/javascript",
        "ask.css": "text/css", "ask.js": "application/javascript",
        "settings.css": "text/css", "settings-ui.js": "application/javascript",
        "hedit.js": "application/javascript", "timeline.js": "application/javascript",
    }
    for _fname, _ctype in _STATIC.items():
        async def _static_file(name: str = _fname, ctype: str = _ctype) -> FileResponse:
            return FileResponse(WEB_DIR / name, media_type=ctype, headers={"Cache-Control": "no-store"})
        app.get(f"/{_fname}", include_in_schema=False)(_static_file)

    # panel 契约的只读数据端点（/api/holdings /api/analysis /api/fundamentals ...）
    # 读 harness 自产 data/panel 与 data/config，前端契约零改动；文件不存在时返回 null（前端 fetchOpt 容忍）
    for _name in PANEL_READONLY:

        async def _panel_file(name: str = _name) -> JSONResponse:
            return JSONResponse(_read_panel(name))

        if _name != "alerts":
            app.get(f"/api/{_name}", include_in_schema=False)(_panel_file)

    @app.get("/api/alerts")
    async def panel_alerts() -> JSONResponse:
        """解析 value_ref，并仅在响应内给旧规则补稳定 ID，不改写配置文件。"""
        from .portfolio_routes import _alert_rules

        cfg = portfolio.resolved_alert_config()
        _alert_rules(cfg)
        return JSONResponse(cfg)

    @app.get("/api/alerts_status")
    async def panel_alerts_status() -> JSONResponse:
        """预警状态：harness 自产（契约映射 hits→triggers）；未跑过检查时返回 null。"""
        local = read_json(DATA_DIR / "alerts_status.json", None)
        if local is None:
            return JSONResponse(None)
        now = time.time()
        triggers = [
            {"code": h.get("code"), "type": h.get("type"), "time": h.get("hit_at", "")}
            for h in (local.get("hits") or [])
            if now - float(h.get("ts") or 0) < 24 * 3600  # 仅展示 24h 内命中，历史进 history
        ]
        return JSONResponse({
            "checked_at": local.get("checked_at") or local.get("updated_at"),
            "triggers": triggers,
            "all_clear": not triggers,
        })

    @app.get("/api/live")
    async def api_live() -> JSONResponse:
        return JSONResponse(read_json(LIVE_FILE, None))

    @app.get("/api/refresh/status")
    async def api_refresh_status() -> dict:
        return live_status()

    _FRESH_TO_JOB = {"analysis": "panel-build", "fundamentals": "panel-build", "research": "panel-build",
                     "events": "panel-build", "earnings": "panel-build", "onepager": "panel-build",
                     "themes": "panel-build", "experts": "experts-refresh", "review": "review-weekly",
                     "alerts": "alert-check", "consensus": "consensus-snapshot"}

    @app.post("/api/refresh")
    async def api_refresh() -> dict:
        """刷新 = 行情快照 + 缺口检查（新增标的未建数 / 数据逾期）→ 后台启动对应任务补齐。"""
        try:
            live = await refresh_live()
        except Exception as e:
            raise HTTPException(502, str(e))
        gaps: dict[str, list] = {"new": [], "stale": []}
        started: list[str] = []
        try:
            from . import freshness as freshness_mod
            from .scheduler import guarded_run
            covered = set((_read_panel("fundamentals").get("items") or {}).keys())
            covered_op = set((_read_panel("onepager").get("items") or {}).keys())
            for it in fr["items"]:
                if it["status"] in ("late", "missing"):
                    job = _FRESH_TO_JOB.get(it["id"])
                    if job and job not in jobs_to_run:
                        jobs_to_run.append(job)
            for job in jobs_to_run:
                asyncio.create_task(guarded_run(job, "refresh-btn"))
                started.append(job)
        except Exception:
            pass  # 缺口检查失败不影响行情刷新结果
        return {**live, "gaps": gaps, "started": started}

    @app.get("/api/notes")
    async def api_notes_get() -> dict:
        return _read_notes()

    @app.get("/api/decide/history")
    async def api_decide_history() -> dict:
        return read_decide_history()

    @app.post("/api/decide/snapshot")
    async def api_decide_snapshot(request: Request) -> JSONResponse:
        body = await request.json()
        async with _DECIDE_LOCK:
            r = save_decide_snapshot(body)
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)

    @app.post("/api/notes")
    async def api_notes_post(request: Request) -> JSONResponse:
        body = await request.json()
        async with _NOTES_LOCK:
            r = mutate_notes(body)
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)

    @app.get("/api/plans")
    async def api_plans_get() -> dict:
        return {"updated_at": _now_iso(), "plans": _read_plans()}

    @app.post("/api/plans")
    async def api_plans_post(request: Request) -> JSONResponse:
        body = await request.json()
        async with _PLANS_LOCK:
            r = mutate_plans(body)
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)

    @app.get("/api/personas")
    async def api_personas() -> JSONResponse:
        return JSONResponse(_read_panel("personas"))

    @app.get("/api/expert")
    async def api_expert_get(code: str = "") -> JSONResponse:
        experts = _read_experts()
        if not code:
            return JSONResponse(experts)
        return JSONResponse({
            "updated_at": experts.get("updated_at"),
            "code": code,
            "results": (experts.get("stocks") or {}).get(code) or {},
        })

    @app.post("/api/expert")
    async def api_expert_post(request: Request) -> JSONResponse:
        body = await request.json()
        try:
            r = await generate_expert(str(body.get("code") or ""), str(body.get("persona") or ""), bool(body.get("force")))
        except HTTPException:
            raise
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
        return JSONResponse(r, status_code=200 if r.get("ok") else 400)
