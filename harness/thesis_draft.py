"""逻辑卡 AI 草稿（移植 8899 thesis_draft.js）。

流程：buildContext 汇总面板本地证据（基本面/研报/专家/量化信号/周更观点/一页纸/组合纪律，
全部带来源标签）→ LLM 输出 JSON 草稿（失败 → 规则兜底）→ sanitize 白名单校验 →
存 data/config/thesis_drafts.json，status=pending；用户在表单确认保存后才成为正式逻辑卡。
计划 Tab 低门槛 AI：quick(code, {kind: judgment|plan, hint}) 只返回不落盘。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from . import llm, portfolio, theses
from .kline import bj_date, bj_full, shift_date
from . import settings
from .store import read_json

SYS = "\n".join([
    "你是一名严谨的价值投资研究助理，为用户起草某只股票的「投资逻辑卡」草稿，供用户人工修改后确认。",
    "硬性要求：",
    "1. 只能使用用户消息中【证据】部分提供的数据，禁止编造任何数字、事件或机构观点；每条理由/证伪条件末尾用方括号标注证据来源标签，如 [基本面]、[研报]、[深度研报]、[专家·巴菲特]、[信号]、[一页纸]。",
    "2. 买入理由写「为什么这门生意值得拥有」（护城河、现金流、成长驱动、估值），3-5 条，每条≤60 字，不写「它会涨」。",
    "3. 证伪条件写「出现什么可观察、可验证的情况就说明逻辑错了」，3-5 条，每条≤60 字，尽量量化（如「连续两个季度营收同比负增长」），并覆盖最主要的风险。",
    "4. 跟踪指标只能从给定的 metric 键中选 3-5 个，阈值需参考当前值给出合理警戒线（不是当前值本身），附一句 why。",
    "5. 目标价：优先参考机构目标均价并做保守折让，给出 target_price 与 target_basis（一句话说明依据）；无依据则填 null。",
    "6. margin_pct 安全边际默认 30（高不确定性可 35-40，稳定现金牛可 20-25）。",
    "7. 只输出一个 JSON 对象，不要 markdown 代码块，不要多余文字。",
    'JSON 结构：{"summary":"一句话逻辑≤60字","reasons":["..."],"falsifiers":["..."],"metrics":[{"metric":"roe","op":"<","value":15,"why":"..."}],"target_price":数字或null,"target_basis":"...","margin_pct":30,"review_date":"YYYY-MM-DD","review_basis":"...","caveats":["数据缺口或需要人工确认的点"]}',
])

QSYS = {
    "judgment": "\n".join([
        "你是投资助理，帮普通投资者把「为什么拿着这只股票」写成一张很短的判断卡。用大白话，不用术语堆砌。",
        "要求：",
        "1. summary：一句话说清为什么持有/关注，≤40 字。用户给了想法时以用户想法为主线，只用证据补充数字，不改变用户的观点方向。",
        "2. falsifiers：2-3 条「出现什么情况说明我判断错了」，每条 ≤30 字，必须能观察、能核对（尽量带数字或时间，如「连续两季营收同比转负」「毛利率跌破 50%」）。",
        "3. review_date：建议复查日（YYYY-MM-DD），有下次财报日就取财报后 3 天，否则 90 天后。",
        "4. basis：一句话写你用到的关键数据（≤50 字）。",
        '5. 只能使用【证据】里的数字，禁止编造。只输出一个 JSON：{"summary":"","falsifiers":[""],"review_date":"","basis":""}',
    ]),
    "plan": "\n".join([
        "你是投资助理，帮普通投资者把一个想法变成 1-2 条可执行的交易计划。用大白话，短句。",
        "每条计划字段：",
        "- title：≤16 字，如「仓位减回 25%」「财报后决定加仓」。",
        "- condition：什么时候启动，≤40 字，必须能明确判断（价格 / 日期 / 财报数据），价格要写具体数字。",
        "- plan：到时我做什么，≤50 字，写清「满足→做什么；不满足→做什么」，涉及数量用仓位百分比或「分 2-3 批」。",
        "- action：buy=建仓 add=加仓 reduce=减仓 sell=卖出 watch=观察 other=其他，选一个。",
        "- deadline：YYYY-MM-DD，今天之后 7-180 天内。",
        "规则：以用户想法为主；用户没写时，从【证据】里挑最紧要的一件事（如仓位超限、临近财报、接近目标价、深度浮亏）。只用证据里的数字，禁止编造。不要和【已有计划】重复。",
        '只输出一个 JSON：{"plans":[{"title":"","condition":"","plan":"","action":"","deadline":""}]}',
    ]),
}
_ACT = {"buy", "add", "reduce", "sell", "watch", "other"}


def _clip(s: Any, n: int) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()[:n]


def _num(x: Any) -> float | None:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _r1(x: float) -> float:
    return round(x * 10) / 10


def _rd(name: str) -> dict | None:
    return read_json(settings.PANEL_OUT_DIR / f"{name}.json", None)


def _has_llm() -> bool:
    from .app_settings import model_for
    try:
        return bool(model_for("draft").get("key"))
    except Exception:
        return False


async def build_context(code: str) -> dict | None:
    st = portfolio.stock_meta(code)
    if not st:
        return None
    q = None
    try:
        from .panel_api import _gtimg
        qs = await _gtimg.get_quotes([st["gtimg"]])
        q = qs.get(st["gtimg"])
    except Exception:
        pass
    fund = _rd("fundamentals") or {}
    res = _rd("research") or {}
    ctx = theses.thesis_ctx(code, st, q, fund, res)
    f = (fund.get("items") or {}).get(code) or {}
    r = (res.get("items") or {}).get(code) or {}
    ex = dict((((_rd("experts") or {}).get("stocks")) or {}).get(code) or {})
    personas = portfolio.load_personas()
    p_name = {p.get("id"): p.get("name", p.get("id")) for p in personas}
    stock_scope = {p.get("id") for p in personas if not p.get("scope") or p.get("scope") == "stock"}
    for pid in list(ex.keys()):
        if stock_scope and pid not in stock_scope:
            ex.pop(pid, None)
    an = _rd("analysis") or {}
    wsig = next((x for x in (an.get("signals") or []) if x.get("code") == code), None)
    rr = None  # 独立版无逐篇分层研报库
    op = ((_rd("onepager") or {}).get("items") or {}).get(code)
    rules = read_json(settings.CONFIG_DIR / "portfolio_rules.json", {}) or {}
    s2 = None
    try:
        from . import signals_service
        s2 = await signals_service.get_signal(code)
    except Exception:
        pass

    ev = []
    L, A, V = f.get("latest") or {}, f.get("annual") or {}, f.get("valuation") or {}
    g = lambda d, k: ("-" if d.get(k) is None else d.get(k))
    if L or A:
        ev.append(f"[基本面] 最新期 {L.get('period') or '?'}：ROE {g(L, 'roe')}%（年化 {g(L, 'roe_annualized')}%）、"
                  f"毛利率 {g(L, 'gross_margin')}%、净利率 {g(L, 'net_margin')}%、营收同比 {g(L, 'rev_yoy')}%、"
                  f"资产负债率 {g(L, 'debt_ratio')}%、ROIC {g(L, 'roic')}%。" + _clip(L.get("note"), 80))
        ev.append(f"[基本面] 年度 {A.get('period') or '?'}：ROE {g(A, 'roe')}%、毛利率 {g(A, 'gross_margin')}%、"
                  f"净利率 {g(A, 'net_margin')}%、营收同比 {g(A, 'rev_yoy')}%、负债率 {g(A, 'debt_ratio')}%。" + _clip(A.get("note"), 80))
    ev.append(f"[估值] 现价 {ctx['price'] if ctx['price'] is not None else '-'} {st.get('currency') or ''}，"
              f"PE(TTM) {ctx['pe'] if ctx['pe'] is not None else '-'}，动态 PE {g(V, 'pe_forward')}，"
              f"PS {g(V, 'ps_ttm')}，PCF {g(V, 'pcf_ttm')}，市值 {g(f, 'market_cap')}{f.get('cap_unit') or ''}{f.get('cap_currency') or ''}")
    if r.get("target_avg"):
        up = f"{_r1(ctx['upside'])}%" if ctx.get("upside") is not None else "-"
        rl = r.get("rating_latest") or {}
        ev.append(f"[研报] {r.get('target_count') or '?'} 家机构目标价均值 {r['target_avg']}"
                  f"（区间 {r.get('target_min')}~{r.get('target_max')}），距现价 {up}"
                  + (f"；评级 买入 {rl.get('buy')}% / 持有 {rl.get('hold')}% / 卖出 {rl.get('sell')}%" if rl else ""))
    nr = r.get("next_report") or {}
    if nr:
        ev.append(f"[研报] 下期 {nr.get('period')}"
                  + (f" 预计 {nr['publish_date']} 披露" if nr.get("publish_date") else "（披露日未定）")
                  + f"，一致预期净利同比 {nr.get('np_yoy')}%、营收同比 {nr.get('rev_yoy')}%")
    for pid, e in ex.items():
        if not e or not e.get("stance"):
            continue
        ev.append(f"[专家·{p_name.get(pid, pid)}] {e['stance']}({e.get('score')})："
                  + "；".join(_clip(p, 90) for p in (e.get("points") or [])[:3])
                  + f"。风险：{_clip(e.get('risk'), 100)}")
    if s2 and (s2.get("state") or {}).get("ok"):
        stx = s2["state"]
        ev.append(f"[价格] 价格状态（仅描述、不预测）：{stx.get('text')}；"
                  + "；".join(f"{x['k']} {x['v']}" for x in (stx.get("items") or []))
                  + f"。纪律闸门「{s2.get('action')}」")
    if wsig:
        ev.append(f"[信号] 周更新观点「{wsig.get('action')}」：" + _clip(wsig.get("summary"), 160)
                  + (" 关注：" + "；".join(_clip(c, 60) for c in (wsig.get("conditions") or [])) if wsig.get("conditions") else ""))
    if op and op.get("sections"):
        for s in (op.get("sections") or [])[:4]:
            p = next((b for b in (s.get("blocks") or []) if b.get("type") == "p" and b.get("text")), None)
            if p:
                ev.append(f"[一页纸·{s.get('title')}] " + _clip(p["text"], 200))
    pos = (f"持仓，成本 {st.get('cost')}，浮盈亏 "
           + (f"{_r1(ctx['pnl_pct'])}%" if ctx.get("pnl_pct") is not None else "-")) if st.get("type") == "holding" else "关注股（未持有）"
    ev.append(f"[组合纪律] 单票上限 {rules.get('single_max', 25)}%、浮亏 {rules.get('stop_review_pct', -50)}% 强制止损审视；"
              "用户价值投资框架：5 年 ROE≥15%、负债率≤60%、安全边际≥30%。")
    return {"st": st, "ctx": ctx, "fund": f, "research": r, "experts": ex, "s2": s2,
            "evidence": ev, "position": pos, "today": bj_date()}


def rule_draft(C: dict) -> dict:
    ctx, r, ex = C["ctx"], C["research"], C["experts"]
    reasons, fals = [], []
    for pid, e in ex.items():
        if e and (e.get("score") or 0) > 0 and e.get("points") and len(reasons) < 4:
            reasons.append(_clip(e["points"][0], 60) + f" [专家·{pid}]")
    if ctx.get("roe") is not None and ctx["roe"] >= 15:
        reasons.append(f"ROE {_r1(ctx['roe'])}% 达到 15% 门槛，资本回报优秀 [基本面]")
    if ctx.get("upside") is not None and ctx["upside"] > 15:
        reasons.append(f"机构目标均价较现价仍有 {_r1(ctx['upside'])}% 空间 [研报]")
    if not reasons:
        reasons.append("（请补充：为什么这门生意值得拥有）")
    for pid, e in ex.items():
        if e and e.get("risk") and len(fals) < 3:
            fals.append("出现：" + _clip(e["risk"], 56) + f" [专家·{pid}]")
    fals.append("连续两个季度营收同比负增长 [基本面]")
    if ctx.get("roe") is not None:
        fals.append(f"年化 ROE 跌破 {max(8, round(min(15, ctx['roe'] * 0.7)))}% 且持续两个季度 [基本面]")
    m = []
    if ctx.get("roe") is not None:
        m.append({"metric": "roe", "op": "<", "value": max(8, round(min(15, ctx["roe"] * 0.75))),
                  "why": f"当前 {_r1(ctx['roe'])}%，跌破说明资本回报恶化"})
    if ctx.get("rev_yoy") is not None:
        m.append({"metric": "rev_yoy", "op": "<", "value": 0 if ctx["rev_yoy"] > 10 else -5,
                  "why": f"当前 {_r1(ctx['rev_yoy'])}%，转负说明增长逻辑受损"})
    if ctx.get("debt_ratio") is not None:
        m.append({"metric": "debt_ratio", "op": ">", "value": min(75, max(60, round(ctx["debt_ratio"] + 10))),
                  "why": f"当前 {_r1(ctx['debt_ratio'])}%，框架上限 60%"})
    if ctx.get("gross_margin") is not None:
        m.append({"metric": "gross_margin", "op": "<", "value": round(ctx["gross_margin"] - 5),
                  "why": f"当前 {_r1(ctx['gross_margin'])}%，下滑 5pct 说明定价权或结构恶化"})
    tgt = round(r["target_avg"] * 0.9 * 100) / 100 if r.get("target_avg") else None
    next_rep = (r.get("next_report") or {}).get("publish_date")
    return {"summary": "（请用一句话写清核心逻辑）", "reasons": reasons[:5], "falsifiers": fals[:5],
            "metrics": m[:5], "target_price": tgt,
            "target_basis": f"机构目标均价 {r['target_avg']} 打九折" if tgt else "无机构目标价，请自行估算",
            "margin_pct": 30,
            "review_date": shift_date(next_rep, 3) if next_rep and next_rep > C["today"] else shift_date(C["today"], 90),
            "review_basis": f"下期财报 {next_rep} 后 3 天复查" if next_rep else "默认 90 天",
            "caveats": ["规则草稿：理由与证伪条件来自专家要点拼接，请务必改写成你自己的判断"]}


def sanitize(j: dict, C: dict) -> dict:
    out = {"summary": _clip(j.get("summary"), 120)}
    out["reasons"] = [x for x in (_clip(v, 200) for v in (j.get("reasons") if isinstance(j.get("reasons"), list) else [])) if x][:5]
    out["falsifiers"] = [x for x in (_clip(v.get("text") if isinstance(v, dict) else v, 200)
                                     for v in (j.get("falsifiers") if isinstance(j.get("falsifiers"), list) else [])) if x][:6]
    out["metrics"] = []
    for m in (j.get("metrics") if isinstance(j.get("metrics"), list) else []):
        if not m or m.get("metric") not in theses.THESIS_METRICS or len(out["metrics"]) >= 5:
            continue
        if any(x["metric"] == m["metric"] for x in out["metrics"]):
            continue
        v = _num(m.get("value"))
        if v is None:
            continue
        out["metrics"].append({"metric": m["metric"], "op": ">" if m.get("op") == ">" else "<",
                               "value": v, "why": _clip(m.get("why"), 80)})
    px = C["ctx"].get("price")
    tp = _num(j.get("target_price"))
    out["target_price"] = round(tp, 2) if tp and (px is None or px * 0.3 < tp < px * 3) else None
    out["target_basis"] = _clip(j.get("target_basis"), 120) or ("" if out["target_price"] else "无可靠依据，未给目标价")
    mg = _num(j.get("margin_pct"))
    out["margin_pct"] = round(mg) if mg is not None and 0 <= mg <= 80 else 30
    rv = str(j.get("review_date") or "")
    max_d = shift_date(C["today"], 365)
    out["review_date"] = rv if re.fullmatch(r"\d{4}-\d{2}-\d{2}", rv) and C["today"] < rv <= max_d else shift_date(C["today"], 90)
    out["review_basis"] = _clip(j.get("review_basis"), 80)
    out["caveats"] = [x for x in (_clip(v, 120) for v in (j.get("caveats") if isinstance(j.get("caveats"), list) else [])) if x][:5]
    out["untagged"] = sum(1 for x in out["reasons"] + out["falsifiers"] if not re.search(r"\[[^\]]+\]\s*$", x))
    return out


async def generate(code: str, opts: dict | None = None) -> dict:
    C = await build_context(code)
    if not C:
        return {"ok": False, "error": "标的不在持仓/关注列表中", "status": 404}
    draft, source, llm_error = None, "rules", None
    if _has_llm() and not (opts or {}).get("rules"):
        user = (f"标的：{C['st'].get('name')}（{code}），{C['position']}；今天 {C['today']}。\n可用跟踪指标 metric 键："
                + "，".join(k + "=" + v["label"] + (f"({v['unit']})" if v["unit"] else "") for k, v in theses.THESIS_METRICS.items())
                + "；当前值：" + "，".join(f"{k}={_r1(C['ctx'][k]) if C['ctx'].get(k) is not None else '无'}"
                                          for k in ("roe", "gross_margin", "net_margin", "rev_yoy", "debt_ratio", "pe", "upside"))
                + "。\n\n【证据】\n" + "\n".join(C["evidence"]))
        try:
            ans = await llm.chat("flash", SYS, user, temperature=0.2, max_tokens=4000, timeout=90, skill="draft")
            m = re.search(r"\{[\s\S]*\}", ans.get("text") or "")
            if not m:
                raise ValueError("LLM 未返回 JSON")
            draft = sanitize(json.loads(m[0]), C)
            if len(draft["reasons"]) < 2 or len(draft["falsifiers"]) < 2:
                raise ValueError("LLM 草稿条目不足")
            source = "llm"
        except Exception as exc:
            llm_error = str(exc)[:160]
            draft = None
    if not draft:
        draft = sanitize(rule_draft(C), C)
    rec = {**draft, "code": code, "name": C["st"].get("name"), "status": "pending",
           "source": source, "llm_error": llm_error, "generated_at": bj_full(),
           "evidence": C["evidence"], "price_at": C["ctx"].get("price")}
    d = theses.read_drafts()
    d["items"][code] = rec
    d["updated_at"] = rec["generated_at"]
    theses.write_drafts(d)
    return {"ok": True, "draft": rec}


# ---- 计划 Tab · 低门槛 AI ----
def _untag(s: Any) -> str:
    return _clip(re.sub(r"(\s*\[[^\]]+\])+\s*$", "", str(s or "")), 200)


def _quick_facts(C: dict) -> dict:
    r = C["research"]
    rules = read_json(settings.CONFIG_DIR / "portfolio_rules.json", {}) or {}
    pos = (C.get("s2") or {}).get("position") or {}
    evs = _rd("events") or {}
    up = sorted((e for e in (evs.get("upcoming_earnings") or [])
                 if e.get("code") == C["st"]["code"] and e.get("date", "") >= C["today"]),
                key=lambda e: e["date"])
    next_earn = (up[0]["date"] if up else None) or (
        (r.get("next_report") or {}).get("publish_date")
        if (r.get("next_report") or {}).get("publish_date", "") >= C["today"] else None)
    th = (theses.read_theses().get("items") or {}).get(C["st"]["code"])
    plans_doc = read_json(settings.DATA_DIR / "plans.json", {}) or {}
    open_plans = [p for p in (plans_doc.get("plans") or []) if p.get("code") == C["st"]["code"] and p.get("status") == "open"]
    return {"held": C["st"].get("type") == "holding", "price": C["ctx"].get("price"),
            "cur": C["st"].get("currency") or "", "cost": C["st"].get("cost"),
            "pnl": C["ctx"].get("pnl_pct"), "weight": pos.get("weight"),
            "lim": rules.get("single_max", 25), "tAvg": r.get("target_avg"),
            "tMin": r.get("target_min"), "nextEarn": next_earn, "th": th, "open": open_plans, "s2": C.get("s2")}


def _quick_user(C: dict, F: dict, kind: str, hint: str) -> str:
    lines = [f"标的：{C['st'].get('name')}（{C['st']['code']}），{C['position']}；今天 {C['today']}。"]
    if F.get("weight") is not None:
        lines.append(f"占全组合 {_r1(F['weight'])}%（单股上限 {F['lim']}%）")
    if F.get("nextEarn"):
        lines.append(f"下次财报：{F['nextEarn']}")
    if F.get("th"):
        th = F["th"]
        lines.append("【我的判断】" + (th.get("summary") or "") + "；证伪条件："
                     + "；".join(f.get("text", "") for f in (th.get("falsifiers") or [])))
    if kind == "plan" and F.get("open"):
        lines.append("【已有计划】" + "；".join(p.get("title", "") for p in F["open"]))
    lines.append("【用户的想法】" + (hint or "（没写，请你根据证据挑最紧要的事）"))
    lines.append("\n【证据】\n" + "\n".join(C["evidence"][:12]))
    return "\n".join(lines)


def _fmt_p(x: float | None) -> str:
    if x is None:
        return ""
    return str(round(x)) if x >= 100 else str(round(x * 100) / 100)


def _rule_judgment(C: dict, F: dict, hint: str) -> dict:
    rd0 = rule_draft(C)
    fals = [x for x in (_untag(f) for f in (rd0.get("falsifiers") or [])) if x and not x.startswith("出现：")][:2]
    if C["ctx"].get("gross_margin") is not None:
        fals.append(f"毛利率跌破 {round(C['ctx']['gross_margin'] - 5)}%（现 {_r1(C['ctx']['gross_margin'])}%）")
    bits = []
    if C["ctx"].get("roe") is not None and C["ctx"]["roe"] >= 15:
        bits.append(f"ROE {_r1(C['ctx']['roe'])}%")
    if C["ctx"].get("upside") is not None:
        bits.append(f"机构目标价空间 {_r1(C['ctx']['upside'])}%")
    summary = _clip(hint, 60) or (
        (f"赚钱能力强（ROE {_r1(C['ctx']['roe'])}%），" if C["ctx"].get("roe") is not None and C["ctx"]["roe"] >= 15 else "")
        + ("价格低于机构预期，愿意长期持有" if C["ctx"].get("upside") is not None and C["ctx"]["upside"] > 15
           else "先观察基本面能否保持"))
    return {"summary": summary, "falsifiers": fals[:3], "review_date": rd0["review_date"],
            "basis": "、".join(bits) or "面板基本面与研报数据"}


def _rule_plans(C: dict, F: dict, hint: str) -> list[dict]:
    out: list[dict] = []
    h = hint or ""
    d = lambda n: shift_date(C["today"], n)
    num0 = (re.search(r"(\d+(?:\.\d+)?)", h) or [None, None])[1]
    if re.search(r"止损|清仓|卖出|卖掉", h):
        want = "sell"
    elif re.search(r"减|降仓|卖一部分|止盈", h):
        want = "reduce"
    elif re.search(r"加仓|补仓|加一点|再买", h):
        want = "add"
    elif re.search(r"建仓|买入|开始买|买一点", h):
        want = "add" if F.get("held") else "buy"
    else:
        want = None
    if h and num0 and want:
        px = float(num0)
        down = bool(re.search(r"跌|以下|低于|回调", h)) or (F.get("price") is not None and px < F["price"])
        act_txt = {"add": f"分 2 批加仓，加后不超过 {F['lim']}%", "buy": "先建 1/3 仓，再跌 10% 补第二批",
                   "reduce": "减掉 1/3，剩余继续持有", "sell": "执行卖出，写复盘"}[want]
        out.append({"title": _clip(h, 16),
                    "condition": f"股价{'跌到' if down else '涨到'} {_fmt_p(px)} {F['cur']}{'以下' if down else '以上'}",
                    "plan": ("触发后先复核判断是否仍成立：成立→" if down else "触发后：") + act_txt + "；未触发→不动",
                    "action": want, "deadline": d(90)})
    if F.get("held") and F.get("weight") is not None and F["weight"] > F["lim"] and not out:
        out.append({"title": f"仓位减回 {F['lim']}%", "condition": f"当前占组合 {_r1(F['weight'])}%，超过上限 {F['lim']}%",
                    "plan": f"分 2 批减到 {F['lim']}% 以内，优先在反弹日执行；减完写复盘", "action": "reduce", "deadline": d(30)})
    if (re.search(r"财报|业绩", h) or (not h and not out)) and F.get("nextEarn"):
        out.append({"title": "财报后再决定", "condition": f"{F['nextEarn']} 财报公布后 3 天内，看营收和净利是否达到一致预期",
                    "plan": "达到或超预期→维持/按计划加仓；明显不及预期→复核我的判断，必要时减仓",
                    "action": "watch", "deadline": shift_date(F["nextEarn"], 5)})
    if not out and F.get("held") and F.get("pnl") is not None and F["pnl"] <= -20:
        out.append({"title": "浮亏处理规则", "condition": f"浮亏已 {_r1(F['pnl'])}%，若跌破 -35% 或判断被证伪",
                    "plan": "判断仍成立→不补仓、继续持有；判断被证伪→分批卖出", "action": "watch", "deadline": d(60)})
    if not out and F.get("tMin") and F.get("price") is not None:
        if F["price"] > F["tMin"]:
            out.append({"title": "回调加仓线" if F.get("held") else "建仓参考线",
                        "condition": f"股价跌到 {_fmt_p(min(F['tMin'], F['price'] * 0.9))} {F['cur']} 以下",
                        "plan": ("分 2 批加仓" if F.get("held") else "先建 1/3 仓") + f"，仓位不超过 {F['lim']}%；未到不动",
                        "action": "add" if F.get("held") else "buy", "deadline": d(90)})
        else:
            out.append({"title": "低于机构最低目标价", "condition": f"现价已低于机构最低目标价 {_fmt_p(F['tMin'])}",
                        "plan": "复核基本面无恶化→分批" + ("加仓" if F.get("held") else "建仓") + "；有恶化→观望",
                        "action": "add" if F.get("held") else "buy", "deadline": d(30)})
    if not out:
        out.append({"title": _clip(h, 16) or "下一步观察", "condition": _clip(h, 40) or "下次复查时",
                    "plan": "到时复核判断与仓位，再决定买卖", "action": "watch", "deadline": d(60)})
    return out[:2]


def _clean_plans(arr: Any, C: dict) -> list[dict]:
    max_d, min_d = shift_date(C["today"], 180), shift_date(C["today"], 1)
    out = []
    for p in (arr if isinstance(arr, list) else []):
        if not p or not _clip(p.get("title"), 30):
            continue
        dl = str(p.get("deadline") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dl) or not min_d <= dl <= max_d:
            dl = shift_date(C["today"], 60)
        out.append({"title": _clip(p.get("title"), 30), "condition": _clip(p.get("condition"), 120),
                    "plan": _clip(p.get("plan"), 160),
                    "action": p["action"] if p.get("action") in _ACT else "watch", "deadline": dl})
        if len(out) >= 2:
            break
    return out


async def quick(code: str, opts: dict | None = None) -> dict:
    opts = opts or {}
    kind = "plan" if opts.get("kind") == "plan" else "judgment"
    hint = _clip(opts.get("hint"), 200)
    C = await build_context(code)
    if not C:
        return {"ok": False, "error": "标的不在持仓/关注列表中", "status": 404}
    F = _quick_facts(C)
    out, source, llm_error = None, "rules", None
    if _has_llm() and not opts.get("rules"):
        try:
            ans = await llm.chat("flash", QSYS[kind], _quick_user(C, F, kind, hint),
                                 temperature=0.2, max_tokens=3000, timeout=90, skill="draft")
            m = re.search(r"\{[\s\S]*\}", ans.get("text") or "")
            if not m:
                raise ValueError("AI 未返回 JSON")
            j = json.loads(m[0])
            if kind == "plan":
                ps = _clean_plans(j.get("plans"), C)
                if not ps:
                    raise ValueError("AI 计划为空")
                out = {"plans": ps}
            else:
                fs0 = [x for x in (_untag(v) for v in (j.get("falsifiers") if isinstance(j.get("falsifiers"), list) else [])) if x][:3]
                if not _clip(j.get("summary"), 80) or not fs0:
                    raise ValueError("AI 判断条目不足")
                rv = str(j.get("review_date") or "")
                out = {"summary": _clip(j.get("summary"), 80), "falsifiers": fs0, "basis": _clip(j.get("basis"), 80),
                       "review_date": rv if re.fullmatch(r"\d{4}-\d{2}-\d{2}", rv) and C["today"] < rv <= shift_date(C["today"], 365)
                       else shift_date(C["today"], 90)}
            source = "llm"
        except Exception as exc:
            llm_error = str(exc)[:120]
            out = None
    if not out:
        out = {"plans": _clean_plans(_rule_plans(C, F, hint), C)} if kind == "plan" else _rule_judgment(C, F, hint)
    return {"ok": True, "kind": kind, "code": code, "name": C["st"].get("name"), "source": source,
            "llm_error": llm_error, "hint": hint, "generated_at": bj_full(), **out}
