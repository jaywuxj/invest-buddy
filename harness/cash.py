"""现金账户与资金汇总（2026-10-20）。

设计口径（用户确认）：
- 现金按币种记录「期初余额 + 校准时点(cash_as_of)」；期初之后的真实交易流水（买入/卖出/分红/入金/出金）自动增减现金，
  期初之前的流水只用于推导持仓、不计入现金推导（流水条数在 ignored_trades 里如实告知）。
- 历史已实现盈亏（此前已清仓、没有系统流水的部分）一次性录入 realized_history（按币种），叠加到已实现合计。
- 总资产 = 持仓市值 + 现金（折人民币）；仓位占比分母为总资产。
- 总收益 = 浮动盈亏 + 已实现盈亏（含分红）。净投入 = 总资产 − 总收益（可由用户覆盖，覆盖值用于交叉核对）。
- 收益率按净投入口径：总收益 ÷ 净投入。

现金为 0 且未配置时，本模块的输出与旧口径完全一致（不改动任何既有结论）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import holdings_service, settings
from .store import atomic_write_json, read_json

CN_TZ = timezone(timedelta(hours=8))
CURRENCIES = ("CNY", "HKD", "USD")
HISTORY_DAYS = 400


def _file() -> Path:
    return settings.DATA_DIR / "cash.json"


def _today() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def _stamp() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def _num(v: Any, name: str, allow_zero: bool = True, allow_negative: bool = False) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 需为数字")
    if not allow_negative and x < 0:
        raise ValueError(f"{name} 不能为负")
    if not allow_zero and x == 0:
        raise ValueError(f"{name} 不能为 0")
    if abs(x) > 1e12:
        raise ValueError(f"{name} 超出合理范围")
    return round(x, 2)


def _empty() -> dict:
    return {"currencies": {}, "flows": [], "realized_history": {},
            "net_invested_override": None, "cash_as_of": None, "updated_at": None, "history": []}


def load() -> dict:
    """读现金配置；损坏/符号链接直接抛错（绝不覆盖用户记录）。"""
    f = _file()
    if not f.exists():
        return _empty()
    if f.is_symlink():
        raise ValueError("cash.json 是符号链接；已停止读取并保留原件")
    try:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
    except Exception:
        raise ValueError("cash.json 无法解析；已停止读取并保留原件（请手动修复后再录入）") from None
    if not isinstance(d, dict):
        raise ValueError("cash.json 结构异常（应为对象）")
    return d


def _save(d: dict) -> None:
    d["updated_at"] = _stamp()
    atomic_write_json(_file(), d)


def _sides_effect(trade: dict) -> tuple[str, float]:
    """一笔真实流水对现金的（币种, 增减额）。"""
    cur = trade.get("currency") or "CNY"
    fee = float(trade.get("fee") or 0)
    side = trade.get("side")
    if side == "buy":
        return cur, -(float(trade.get("price") or 0) * float(trade.get("shares") or 0) + fee)
    if side == "sell":
        return cur, float(trade.get("price") or 0) * float(trade.get("shares") or 0) - fee
    if side == "dividend":
        return cur, float(trade.get("amount") or 0) - fee
    return cur, 0.0


def balances(d: dict | None = None, trades: list[dict] | None = None) -> dict:
    """当前各币种现金 = 期初余额 + cash_as_of 之后的真实流水 + 入出金。"""
    d = d if d is not None else load()
    if trades is None:
        trades = (holdings_service.list_trades().get("items") or [])
    as_of = d.get("cash_as_of") or ""
    out = {c: float((d.get("currencies") or {}).get(c) or 0) for c in CURRENCIES}
    for k, v in (d.get("currencies") or {}).items():
        if k not in out:
            out[k] = float(v or 0)
    ignored = 0
    for t in trades:
        if t.get("side") == "init":
            continue
        if as_of and str(t.get("date") or "") < as_of:
            ignored += 1
            continue
        cur, delta = _sides_effect(t)
        out[cur] = out.get(cur, 0.0) + delta
    for f in d.get("flows") or []:
        if as_of and str(f.get("date") or "") < as_of:
            continue
        cur = f.get("currency") or "CNY"
        amt = float(f.get("amount") or 0)
        out[cur] = out.get(cur, 0.0) + (amt if f.get("dir") == "in" else -amt)
    return {k: round(v, 2) for k, v in out.items()}, ignored


def nav_history() -> list[dict]:
    d = load()
    h = d.get("history") or []
    return h if isinstance(h, list) else []


def record_nav(total_assets: float, cash: float, mv: float) -> bool:
    """每个交易日记一条总资产快照（同一天覆盖），供后续时间加权收益率与回撤使用。"""
    if not total_assets or total_assets <= 0:
        return False
    d = load()
    h = [x for x in (d.get("history") or []) if isinstance(x, dict)]
    today = _today()
    row = {"d": today, "assets": round(total_assets, 2), "cash": round(cash, 2), "mv": round(mv, 2)}
    prev = next((x for x in h if x.get("d") == today), None)
    if prev and abs(float(prev.get("assets") or 0) - row["assets"]) < 0.01:
        return False            # 当日数据未变化，不重复写盘
    h = [x for x in h if x.get("d") != today] + [row]
    h.sort(key=lambda x: x.get("d") or "")
    d["history"] = h[-HISTORY_DAYS:]
    _save(d)
    return True


def realized_history_total() -> dict:
    d = load()
    return {k: float(v or 0) for k, v in (d.get("realized_history") or {}).items()}


def summary(fx_rates: dict | None = None) -> dict:
    """资金概况：现金（原币与折人民币）、历史已实现、净投入覆盖值。"""
    d = load()
    bal, ignored = balances(d)
    rates = fx_rates or _fx()
    cny = 0.0
    detail = []
    for c, v in bal.items():
        r = 1.0 if c == "CNY" else float(rates.get(c) or 0)
        cv = v * r
        cny += cv
        detail.append({"currency": c, "amount": v, "rate": r, "cny": round(cv, 2)})
    detail.sort(key=lambda x: -x["cny"])
    hist = realized_history_total()
    hist_cny = sum(v * (1.0 if c == "CNY" else float(rates.get(c) or 0)) for c, v in hist.items())
    return {"cash_by_currency": detail, "cash_cny": round(cny, 2),
            "realized_history": hist, "realized_history_cny": round(hist_cny, 2),
            "net_invested_override": d.get("net_invested_override"),
            "cash_as_of": d.get("cash_as_of"), "updated_at": d.get("updated_at"),
            "ignored_trades": ignored, "configured": bool(d.get("currencies") or d.get("flows") or hist)}


def realized_from_trades(rates: dict | None = None) -> dict:
    """按币种汇总「交易流水」产生的已实现盈亏（含分红），并折人民币。"""
    _, _, by = holdings_service.trade_snapshot()
    rates = rates or _fx()
    per: dict[str, float] = {}
    cny = 0.0
    for s in by.values():
        cur = s.get("currency") or "CNY"
        r = 1.0 if cur == "CNY" else float(rates.get(cur) or 0)
        v = float(s.get("realized") or 0)
        per[cur] = round(per.get(cur, 0.0) + v, 2)
        cny += v * r
    return {"by_currency": per, "cny": round(cny, 2)}


def compose(market_value: float, total_return: float, rates: dict | None = None, with_nav: bool = True) -> dict:
    """资金总账（核心）：已知市值与「总收益」时组装全部派生口径。

    总收益 = 浮动盈亏 + 已实现（交易流水 + 历史一次性录入）
    净投入 = 总资产 − 总收益；收益率按净投入口径
    """
    rates = rates or _fx()
    cs = summary(rates)
    cash_cny = round(float(cs.get("cash_cny") or 0), 2)
    hist_cny = round(float(cs.get("realized_history_cny") or 0), 2)
    traded = realized_from_trades(rates)
    realized_cny = round(float(traded.get("cny") or 0) + hist_cny, 2)
    mv = round(float(market_value or 0), 2)
    total_return = round(float(total_return or 0), 2)
    un = round(total_return - realized_cny, 2)
    total_assets = round(mv + cash_cny, 2)
    net_invested = round(total_assets - total_return, 2)
    override = cs.get("net_invested_override")
    base = round(float(override), 2) if override else net_invested
    out = {**cs, "realized_trades": traded, "realized_cny": realized_cny,
           "market_value": mv, "unrealized_cny": un, "total_assets": total_assets,
           "total_return_cny": total_return, "net_invested_cny": net_invested,
           "net_invested_used": base,
           "return_pct": round(total_return / base * 100, 2) if base > 0 else None,
           "cash_weight": round(cash_cny / total_assets * 100, 2) if total_assets > 0 else 0.0,
           "check_diff": round(net_invested - base, 2) if override else None,
           "denominator": total_assets}
    if with_nav and total_assets > 0:
        try:
            record_nav(total_assets, cash_cny, mv)
        except Exception:
            pass                                   # 快照失败不影响主流程
        out["nav_history"] = nav_history()
    return out


def funds(market_value: float, unrealized: float, rates: dict | None = None, with_nav: bool = True) -> dict:
    """资金总账：入参是「浮动盈亏」时用它（内部补上已实现）。"""
    rates = rates or _fx()
    extra = round(float(realized_from_trades(rates).get("cny") or 0)
                  + float(summary(rates).get("realized_history_cny") or 0), 2)
    return compose(market_value, float(unrealized or 0) + extra, rates, with_nav=with_nav)


def _fx() -> dict:
    live = read_json(settings.DATA_DIR / "live.json", {}) or {}
    rates = live.get("fx") or {}
    out = {"CNY": 1.0}
    for c, r in rates.items():
        try:
            if float(r) > 0:
                out[c] = float(r)
        except (TypeError, ValueError):
            continue
    return out


def mutate(body: dict) -> dict:
    """op: set_balance / calibrate / flow_add / flow_delete / set_realized / set_net_invested"""
    d = load()
    op = str(body.get("op") or "")
    if op in ("set_balance", "calibrate"):
        cur = {c: _num((body.get("currencies") or {}).get(c) or 0, f"{c} 现金") for c in CURRENCIES}
        d["currencies"] = cur
        d["cash_as_of"] = body.get("as_of") or _today()
        if op == "calibrate":
            d["calibrated_at"] = _stamp()
        _save(d)
        return {"ok": True, "op": op, "currencies": cur, "cash_as_of": d["cash_as_of"]}
    if op == "flow_add":
        cur = str(body.get("currency") or "CNY").upper()
        if cur not in CURRENCIES:
            raise ValueError("币种须为 CNY / HKD / USD")
        direction = str(body.get("dir") or "")
        if direction not in ("in", "out"):
            raise ValueError("dir 须为 in（入金）或 out（出金）")
        flow = {"id": "c" + str(int(time.time() * 1000))[-12:], "date": body.get("date") or _today(),
                "currency": cur, "dir": direction, "amount": _num(body.get("amount"), "金额", allow_zero=False),
                "note": str(body.get("note") or "")[:80], "created_at": _stamp()}
        d.setdefault("flows", []).append(flow)
        _save(d)
        return {"ok": True, "flow": flow}
    if op == "flow_delete":
        fid = str(body.get("id") or "")
        before = len(d.get("flows") or [])
        d["flows"] = [f for f in (d.get("flows") or []) if f.get("id") != fid]
        if len(d["flows"]) == before:
            raise ValueError("未找到该资金流水")
        _save(d)
        return {"ok": True, "deleted": fid}
    if op == "set_realized":
        # 历史已实现盈亏可以是亏损（负数）
        hist = {c: _num((body.get("realized") or {}).get(c) or 0, f"{c} 历史已实现", allow_negative=True)
                for c in CURRENCIES}
        d["realized_history"] = hist
        _save(d)
        return {"ok": True, "realized_history": hist}
    if op == "set_net_invested":
        v = body.get("net_invested")
        d["net_invested_override"] = None if v in (None, "", 0) else _num(v, "累计净投入", allow_zero=False)
        _save(d)
        return {"ok": True, "net_invested_override": d["net_invested_override"]}
    raise ValueError("不支持的现金操作")
