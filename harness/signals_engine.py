"""市场侧价格引擎。

2026-10-14 方法论调整（用户确认）：全组合回测显示价格方向信号没有可验证的预测力
（独立样本扣基准后超额≈0，置信度分档未校准），因此：
  · 面板只展示 price_state()：趋势 / 一年区间位置 / RSI 超买超卖 / 近期涨跌 —— 客观描述，不做预测；
  · 仓位纪律闸门 position_action() 只看纪律（证伪、红线、仓位上限、深度浮亏、安全边际买入价），不再读方向；
  · price_factors()/combine() 保留，仅供信号账本与方法检验持续前向记录（看它日后是否显出有效性）。
账本：data/panel/signal_ledger.json，每个自然日每只标的一条。
"""
from __future__ import annotations

import math
from typing import Any

from . import settings
from .store import atomic_write_json, read_json

W = {"trend": 0.45, "momentum": 0.30, "position": 0.25}
LABEL = {"trend": "趋势", "momentum": "动量", "position": "位置"}
MODEL = "price3"
TH = 0.2


def _r2(x):
    return None if x is None or not math.isfinite(x) else round(x, 2)


def _clamp(x, a, b):
    return max(a, min(b, x))


def _sma(a, n, end):
    if end + 1 < n:
        return None
    return sum(a[end - n + 1: end + 1]) / n


def _rsi(a, n, end):
    if end < n:
        return None
    g = l = 0.0
    for i in range(end - n + 1, end + 1):
        d = a[i] - a[i - 1]
        if d > 0:
            g += d
        else:
            l -= d
    return 50.0 if g + l == 0 else 100 * g / (g + l)


def price_factors(closes: list[float], end: int) -> dict:
    """价格因子（可在历史任意一天 end 上计算，供回测复用）。不含文字解读（前端展示用 txt 从简）。"""
    out: dict[str, dict] = {}
    c = closes[end]
    m20, m60, m20p = _sma(closes, 20, end), _sma(closes, 60, end), _sma(closes, 20, end - 5)
    if m20 is not None and m60 is not None and m20p is not None:
        s = 0.0
        s += 0.35 if c > m20 else -0.35
        s += 0.35 if m20 > m60 else -0.35
        s += _clamp(((m20 / m20p - 1) * 100) / 3, -0.3, 0.3)
        out["trend"] = {"s": _clamp(s, -1, 1),
                        "txt": f"现价{'在' if c > m20 else '跌破'} MA20，MA20 {'>' if m20 > m60 else '<'} MA60，"
                               f"MA20 五日斜率 {(m20 / m20p - 1) * 100:.1f}%"}
    if end >= 60:
        r20 = (c / closes[end - 20] - 1) * 100
        r60 = (c / closes[end - 60] - 1) * 100
        out["momentum"] = {"s": _clamp(r20 / 20 * 0.5 + r60 / 40 * 0.5, -1, 1),
                           "txt": f"近 20 日 {r20:+.1f}%，近 60 日 {r60:+.1f}%"}
    r14 = _rsi(closes, 14, end)
    if r14 is not None and end >= 120:
        hi = max(closes[max(0, end - 249): end + 1])
        lo = min(closes[max(0, end - 249): end + 1])
        pos = (c - lo) / (hi - lo) if hi > lo else 0.5
        s = (0.6 if r14 <= 30 else -0.6 if r14 >= 70 else (50 - r14) / 50) + (0.5 - pos) * 0.4
        out["position"] = {"s": _clamp(s, -1, 1),
                           "txt": f"RSI14 {r14:.0f}" + ("（超卖）" if r14 <= 30 else "（超买）" if r14 >= 70 else "")
                                  + f"，处于一年区间 {pos * 100:.0f}% 分位"}
    return out


def price_state(closes: list[float], end: int) -> dict:
    """价格状态（纯描述，不预测）：每一项都给原始数字与判定规则，前端原样展示。"""
    if end < 0:
        return {"ok": False, "text": "暂无日 K 数据", "items": []}
    c = closes[end]
    items: list[dict] = []
    tags: list[str] = []
    num: dict = {}  # 数值字段：前端按模板拼大白话用（文字由程序生成，不交给模型）
    m20, m60 = _sma(closes, 20, end), _sma(closes, 60, end)
    if m20 is not None and m60 is not None:
        if c > m20 > m60:
            t = "上升趋势"
        elif c < m20 < m60:
            t = "下降趋势"
        else:
            t = "震荡"
        tags.append(t)
        num["trend"] = t
        items.append({"k": "趋势", "v": t, "raw": f"现价 {c:.2f} · MA20 {m20:.2f} · MA60 {m60:.2f}",
                      "rule": "现价 > MA20 > MA60 为上升趋势；现价 < MA20 < MA60 为下降趋势；其余为震荡"})
    if end >= 120:
        win = closes[max(0, end - 249): end + 1]
        hi, lo = max(win), min(win)
        pos = (c - lo) / (hi - lo) * 100 if hi > lo else 50.0
        z = "一年低位" if pos <= 20 else "一年高位" if pos >= 80 else "区间中部"
        tags.append(z)
        num.update({"pos": round(pos, 1), "lo": round(lo, 2), "hi": round(hi, 2)})
        items.append({"k": "一年区间位置", "v": f"{pos:.0f}%（{z}）", "raw": f"近 {len(win)} 个交易日 最低 {lo:.2f} · 最高 {hi:.2f}",
                      "rule": "0% = 一年最低价，100% = 一年最高价；≤20% 为低位，≥80% 为高位"})
    r14 = _rsi(closes, 14, end)
    if r14 is not None:
        o = "超卖" if r14 <= 30 else "超买" if r14 >= 70 else "正常"
        if o != "正常":
            tags.append(o)
        num["rsi"] = round(r14, 1)
        items.append({"k": "RSI14", "v": f"{r14:.0f}（{o}）", "raw": "近 14 个交易日上涨幅度占总波动的比例",
                      "rule": "≥70 为超买、≤30 为超卖，只说明近期涨跌是否过快，不代表会反转"})
    if end >= 60:
        r20 = (c / closes[end - 20] - 1) * 100
        r60 = (c / closes[end - 60] - 1) * 100
        num.update({"r20": round(r20, 1), "r60": round(r60, 1)})
        items.append({"k": "近期涨跌", "v": f"20 日 {r20:+.1f}% · 60 日 {r60:+.1f}%", "raw": "按收盘价计算（前复权）",
                      "rule": "仅描述已发生的涨跌"})
    return {"ok": bool(items), "text": " · ".join(tags[:2]) if tags else "数据不足", "items": items, **num}


def combine(fac: dict) -> dict:
    ws = ss = 0.0
    w_all = sum(W.values())
    for k, w in W.items():
        if k in fac:
            ws += w
            ss += w * fac[k]["s"]
    d = ss / ws if ws else 0.0
    label = "看多" if d >= TH else "看空" if d <= -TH else "中性"
    against = 0.0
    if label != "中性":
        for k in fac:
            s = fac[k]["s"]
            if (s > 0) != (d > 0) and abs(s) >= 0.15:
                against += W[k]
    coverage = ws / w_all
    agree = 0.5 if label == "中性" else _clamp(1 - (against / ws if ws else 0) * 1.5, 0, 1)
    cv = coverage * agree * (1 if label == "中性" else _clamp(0.6 + abs(d), 0, 1))
    return {"dir": _r2(d), "label": label, "conf": _r2(cv),
            "conf_level": "hi" if cv >= 0.6 else "mid" if cv >= 0.35 else "lo",
            "coverage": _r2(coverage)}


def market_signal(rows: list[dict]) -> dict:
    """单股市场侧信号：rows 为日 K [{d,c,...}] 升序。"""
    closes = [x["c"] for x in rows]
    end = len(closes) - 1
    fac = price_factors(closes, end) if end >= 60 else {}
    cmb = combine(fac)
    # 始终输出三因子骨架（无数据时 s=null/txt=无数据），与前端契约一致
    factors = [{"key": k, "name": LABEL[k], "w": W[k],
                "s": _r2(fac[k]["s"]) if k in fac else None,
                "txt": fac[k]["txt"] if k in fac else "无数据", "d": None} for k in W]
    return {**cmb, "factors": factors, "close": closes[end] if closes else None,
            "kline_date": rows[end]["d"] if rows else None,
            "state": price_state(closes, end)}


def position_action(m: dict, p: dict, rules: dict | None = None) -> dict:
    """仓位纪律闸门（只看纪律，不读价格方向；m 参数保留以兼容调用方）。
    持仓：证伪 > −50% 红线 > 超单股上限 > 深度浮亏 > 接近预警线 > 未拦。
    关注：证伪 > 未到自设安全边际买入价 > 未拦。allow_add 表示纪律是否放行加仓/建仓。"""
    R = {"single_max": 25, "single_warn": 20, "stop_review_pct": -50, "deep_loss_pct": -40}
    R.update(rules or {})
    if not p.get("held"):
        if p.get("falsified"):
            return {"action": "放弃", "level": "watch-close", "allow_add": False,
                    "why": [f"「我的判断」证伪条件已命中 {p['falsified']} 条"]}
        if p.get("margin_ok") is False:
            return {"action": "未到买入价", "level": "neutral", "allow_add": False,
                    "why": ["现价高于你在逻辑卡里设的安全边际买入价"]}
        return {"action": "未拦", "level": "bullish", "allow_add": True,
                "why": ["无证伪、无买入价限制" if p.get("has_thesis") else "无证伪；还没写逻辑卡，建议先写清买入理由再建仓"]}
    w = p.get("weight") or 0
    pnl = p.get("pnl_pct") or 0
    if p.get("falsified"):
        return {"action": "止损审视", "level": "watch-close", "allow_add": False,
                "why": [f"「我的判断」证伪条件已命中 {p['falsified']} 条（纪律优先于观点）"]}
    if pnl <= R["stop_review_pct"]:
        return {"action": "止损审视", "level": "watch-close", "allow_add": False,
                "why": [f"浮亏 {pnl:.1f}% 已破 {R['stop_review_pct']}% 红线"]}
    if w > R["single_max"]:
        return {"action": "减仓", "level": "watch-close", "allow_add": False,
                "why": [f"仓位 {w:.1f}% 超单股上限 {R['single_max']}%，先回到上限内"]}
    if pnl <= R["deep_loss_pct"]:
        return {"action": "暂停加仓", "level": "neutral", "allow_add": False,
                "why": [f"浮亏 {pnl:.1f}% 超 {R['deep_loss_pct']}% 深度浮亏线",
                        "补仓前先复核「我的判断」是否仍成立" if p.get("has_thesis") else "还没写「我的判断」：先写清为什么拿着、什么情况算错"]}
    if w >= R["single_warn"]:
        return {"action": "接近上限", "level": "normal", "allow_add": False,
                "why": [f"仓位 {w:.1f}% 已到预警线 {R['single_warn']}%（上限 {R['single_max']}%），不再加仓"]}
    return {"action": "未拦", "level": "bullish", "allow_add": True,
            "why": [f"仓位 {w:.1f}% 低于预警线 {R['single_warn']}%，浮盈亏 {pnl:+.1f}% 未触及纪律线"]}


# ---- 账本 ----
def _ledger_file() -> Path:
    return settings.PANEL_OUT_DIR / "signal_ledger.json"


def read_ledger() -> dict:
    d = read_json(_ledger_file(), {"updated_at": None, "entries": []})
    return d if isinstance(d.get("entries"), list) else {"updated_at": None, "entries": []}


def record_ledger(today: str, items: list[dict]) -> dict:
    L = read_ledger()
    idx = {f"{e.get('date')}|{e.get('code')}": i for i, e in enumerate(L["entries"])}
    for s in items:
        e = {"date": today, "code": s.get("code"), "dir": s.get("dir"), "label": s.get("label"),
             "conf": s.get("conf"), "close": s.get("close"), "kline_date": s.get("kline_date"),
             "action": s.get("action"), "model": MODEL}
        key = f"{today}|{s.get('code')}"
        if key in idx:
            L["entries"][idx[key]] = e
        else:
            L["entries"].append(e)
    L["updated_at"] = today
    atomic_write_json(_ledger_file(), L)
    return L
