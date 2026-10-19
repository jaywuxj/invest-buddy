"""交易前纪律检查（逐行移植 8899 discipline.js）。

evaluate({action, code, amount_cny, diag, quote, bars, thesis, trades, plans, rules, today})
  → {level: ok|amber|red, items: [{key,level,dim,title,detail}], need_reason, min_reason, post_weight}
买/加仓（buy/add）与卖/减仓（sell/reduce）走不同检查集；watch/other 只做冲动检查。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

DEFAULTS = {
    "chase_day_amber": 5, "chase_day_red": 8, "chase_5d_amber": 15,
    "panic_day_amber": -5, "cooldown_days": 3,
    "reason_min_amber": 10, "reason_min_red": 20, "deep_loss_add_red": True,
}
BUY = ("buy", "add")
SELL = ("sell", "reduce")
ACTIONS = {"buy": "建仓", "add": "加仓", "reduce": "减仓", "sell": "清仓/卖出", "watch": "观察", "other": "其他"}
_RANK = {"ok": 0, "amber": 1, "red": 2}


def _r1(v: float) -> float:
    return round(v * 10) / 10


def _chg(bars: list[dict], n: int) -> float | None:
    if not bars or len(bars) <= n:
        return None
    a, b = bars[-1 - n]["c"], bars[-1]["c"]
    return (b / a - 1) * 100 if a > 0 else None


def _days_between(a: str, b: str) -> int:
    da = datetime.strptime(str(a)[:10], "%Y-%m-%d")
    db = datetime.strptime(str(b)[:10], "%Y-%m-%d")
    return round((db - da).total_seconds() / 86400)


def evaluate(x: dict) -> dict:
    R = {**DEFAULTS, **((x.get("rules") or {}).get("discipline") or {})}
    base = x.get("rules") or {}
    action = x.get("action") if x.get("action") in ACTIONS else "other"
    items: list[dict] = []

    def add(level, key, dim, title, detail):
        items.append({"level": level, "key": key, "dim": dim, "title": title, "detail": detail})

    diag = x.get("diag") or {"rows": [], "totals": {"mv": 0}}
    row = next((r for r in (diag.get("rows") or []) if r.get("code") == x.get("code")), None)
    tot_mv = (diag.get("totals") or {}).get("mv") or 0
    amt = float(x["amount_cny"]) if x.get("amount_cny") and float(x["amount_cny"]) > 0 else 0
    is_buy, is_sell = action in BUY, action in SELL
    q = x.get("quote") or {}
    day_pct = float(q["pct"]) if q.get("pct") is not None else None
    d5 = _chg(x.get("bars") or [], 5)
    post_weight = (row or {}).get("weight") or 0

    if is_buy:
        mv = (row or {}).get("mv") or 0
        post_weight = (mv + amt) / (tot_mv + amt) * 100 if tot_mv + amt > 0 else 0
        w_txt = (f"加仓 ¥{round(amt):,} 后占组合 {_r1(post_weight)}%"
                 if amt else f"当前占组合 {_r1(post_weight)}%（未填金额，按现仓位估算）")
        if base.get("single_max") is not None and post_weight > base["single_max"]:
            add("red", "conc", "集中度", "单股仓位超红线", w_txt + f"，超出单股上限 {base['single_max']}%。")
        elif base.get("single_warn") is not None and post_weight > base["single_warn"]:
            add("amber", "conc", "集中度", "单股仓位接近红线", w_txt + f"，预警线 {base['single_warn']}%。")
        else:
            add("ok", "conc", "集中度", "仓位在限内", w_txt + "。")
        grp = (row or {}).get("group") or (x.get("st") or {}).get("group") or (x.get("st") or {}).get("sector")
        if grp and base.get("sector_max") is not None:
            gmv = sum(r.get("mv") or 0 for r in (diag.get("rows") or []) if r.get("group") == grp)
            gw = (gmv + amt) / (tot_mv + amt) * 100 if tot_mv + amt > 0 else 0
            if gw > base["sector_max"]:
                add("amber", "sector", "集中度", f"行业「{grp}」将达 {_r1(gw)}%",
                    f"超出行业上限 {base['sector_max']}%，同一景气/监管冲击会同时命中。")
        if day_pct is not None and day_pct > R["chase_day_red"]:
            add("red", "chase", "追高", f"今日已涨 {_r1(day_pct)}%",
                f"单日涨幅超过 {R['chase_day_red']}%，情绪驱动概率高，建议等回落或分批。")
        elif day_pct is not None and day_pct > R["chase_day_amber"]:
            add("amber", "chase", "追高", f"今日已涨 {_r1(day_pct)}%",
                f"单日涨幅超过 {R['chase_day_amber']}%，确认不是追涨。")
        elif d5 is not None and d5 > R["chase_5d_amber"]:
            add("amber", "chase", "追高", f"近 5 日累计涨 {_r1(d5)}%",
                f"短期涨幅超过 {R['chase_5d_amber']}%，注意均值回归。")
        else:
            add("ok", "chase", "追高", "无追高迹象",
                f"今日 {_r1(day_pct) if day_pct is not None else '—'}%，近 5 日 {_r1(d5) if d5 is not None else '—'}%。")
        t = x.get("thesis")
        ev = (t or {}).get("eval")
        if not t:
            add("red", "thesis", "逻辑卡", "没有投资逻辑卡",
                "买入前先写清买入理由、证伪条件和目标价（个股页「计划」Tab，可 AI 起草）。")
        elif ev and ev.get("falsified"):
            add("red", "thesis", "逻辑卡", f"逻辑卡已有 {ev['falsified']} 条证伪",
                "证伪条件已命中，加仓等于逆着自己的逻辑下注。")
        elif ev and ev.get("margin_ok") is False:
            add("amber", "thesis", "逻辑卡", "价格高于安全边际买入价",
                f"现价 {ev.get('price')} > 买入价 {ev.get('buy_below')}（目标价 × (1 − 安全边际)）。")
        else:
            add("ok", "thesis", "逻辑卡", "逻辑卡有效",
                f"现价 {ev.get('price')} ≤ 买入价 {ev.get('buy_below')}。" if ev and ev.get("buy_below") else "逻辑卡存在且未证伪。")
        if row and action == "add" and R["deep_loss_add_red"] and (row.get("pnl_pct") or 0) <= (base.get("deep_loss_pct") or -40):
            add("red", "deep", "摊薄", f"浮亏 {_r1(row['pnl_pct'])}% 时加仓",
                f"已低于深度浮亏线 {base.get('deep_loss_pct')}%，先复核逻辑是否证伪，避免为摊低成本而加仓。")
        sa = (x.get("signal") or {}).get("action")
        if sa and re_search(sa):
            add("amber", "signal", "纪律", f"仓位纪律闸门「{sa}」",
                "纪律闸门（证伪 / 红线 / 仓位上限）不支持加仓，确认你有充分理由再执行。")
    elif is_sell:
        if day_pct is not None and day_pct < R["panic_day_amber"]:
            add("amber", "panic", "恐慌", f"今日已跌 {_r1(day_pct)}%",
                "单日急跌中卖出，确认是逻辑变化而不是情绪。")
        else:
            add("ok", "panic", "恐慌", "无急跌", f"今日 {_r1(day_pct) if day_pct is not None else '—'}%。")
        if not x.get("thesis"):
            add("amber", "thesis", "逻辑卡", "没有逻辑卡可对照",
                "卖出理由无法与当初买入理由对照，建议先补逻辑卡或在理由里写清。")
        else:
            add("ok", "thesis", "逻辑卡", "将关联逻辑卡", "卖出理由会与逻辑卡一起存档，便于复盘。")
    recent_t = [t for t in (x.get("trades") or [])
                if t.get("code") == x.get("code") and t.get("side") != "init"
                and t.get("date") and _days_between(t["date"], x["today"]) < R["cooldown_days"]]
    recent_p = [p for p in (x.get("plans") or [])
                if p.get("code") == x.get("code") and p.get("status") == "open" and p.get("created_at")
                and _days_between(str(p["created_at"])[:10], x["today"]) < R["cooldown_days"]
                and (not p.get("action") or p.get("action") == action)]
    if recent_t or recent_p:
        parts = []
        if recent_t:
            parts.append(f"近 {R['cooldown_days']} 天已有 {len(recent_t)} 笔交易（最近 {recent_t[-1].get('date')}）")
        if recent_p:
            parts.append(f"已有 {len(recent_p)} 条同向计划")
        add("amber", "cool", "冷静期", f"距上次操作不足 {R['cooldown_days']} 天",
            "；".join(parts) + "。频繁操作通常损害收益。")
    else:
        add("ok", "cool", "冷静期", "无近期操作", f"距上次操作 ≥ {R['cooldown_days']} 天。")
    level = "ok"
    for i in items:
        if _RANK[i["level"]] > _RANK[level]:
            level = i["level"]
    return {"ok": True, "action": action, "action_label": ACTIONS[action], "code": x.get("code"),
            "level": level, "items": items, "need_reason": level != "ok",
            "min_reason": R["reason_min_red"] if level == "red" else R["reason_min_amber"] if level == "amber" else 0,
            "post_weight": _r1(post_weight), "amount_cny": amt or None, "rules": R}


def re_search(action: str) -> bool:
    import re
    return bool(re.search(r"减仓|止损|卖出|回避", action))


def gate(check: dict | None, reason: str) -> dict | None:
    """理由是否满足：返回 None=通过，否则错误对象。"""
    if not check or check.get("level") == "ok":
        return None
    n = len(str(reason or "").strip())
    if n >= check.get("min_reason", 0):
        return None
    return {"ok": False, "error": "discipline_ack_required",
            "message": ("红牌" if check["level"] == "red" else "黄牌")
            + f"：需填写不少于 {check['min_reason']} 字的理由才能保存", "check": check}
