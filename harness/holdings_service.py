"""本地组合写入层：持仓与流水共享锁，沿用 config/holdings.json 原有结构。"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import settings


_LOCK = threading.RLock()
_WRITE_STATE = threading.local()
_TZ = timezone(timedelta(hours=8))
_SELL_REASONS = ["估值到位", "逻辑证伪", "更好机会", "风控/止损", "仓位再平衡", "资金需求", "其他"]
_SIDES = {"init": "期初持仓", "buy": "买入", "sell": "卖出", "dividend": "分红", "split": "拆/合股"}
_MARKETS = {
    "HK": ("港股", "HKD", "hk"), "US": ("美股", "USD", "us"),
    "SH": ("A股", "CNY", "sh"), "SZ": ("A股", "CNY", "sz"), "BJ": ("A股", "CNY", "bj"),
}


class PortfolioError(ValueError):
    def __init__(self, error: str, message: str | None = None):
        self.error = error
        self.message = message or error
        super().__init__(self.message)


def _now() -> datetime:
    return datetime.now(_TZ)


def _date() -> str:
    return _now().date().isoformat()


def _stamp() -> str:
    return _now().isoformat(timespec="seconds")


def _holdings_file() -> Path:
    return settings.CONFIG_DIR / "holdings.json"


def _trades_file() -> Path:
    return settings.DATA_DIR / "trades.json"


def _reject_constant(value: str) -> None:
    raise ValueError(f"非法 JSON 数值: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"重复 JSON 字段: {key}")
        data[key] = value
    return data


def _valid_lists(data: object, lists: tuple[str, ...]) -> bool:
    return (isinstance(data, dict) and all(isinstance(data.get(key), list) for key in lists)
            and all(isinstance(row, dict) for key in lists for row in data[key]))


def _strict_json(path: Path, lists: tuple[str, ...]) -> dict:
    if path.is_symlink():
        raise PortfolioError("corrupt_data", f"{path.name} 是符号链接；已停止写入并保留原件")
    if not path.exists():
        return {key: [] for key in lists}
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant,
                          object_pairs_hook=_unique_object)
        json.dumps(data, allow_nan=False)
    except (OSError, UnicodeError, ValueError) as exc:
        raise PortfolioError("corrupt_data", f"{path.name} 无法读取或解析；已停止写入并保留原件") from exc
    if not _valid_lists(data, lists):
        raise PortfolioError("corrupt_data", f"{path.name} 缺少必需数组或记录格式异常；已停止写入并保留原件")
    return data


def _valid_holdings(data: dict) -> bool:
    return all(isinstance(row.get("code"), str) and row["code"]
               for key in ("positions", "watch") for row in data[key])


def _valid_trades(data: dict) -> bool:
    for row in data["items"]:
        if (not isinstance(row.get("side"), str) or row["side"] not in _SIDES
                or any(not isinstance(row.get(key), str) or not row[key]
                       for key in ("code", "name", "currency", "date"))
                or not isinstance(row.get("created_at", ""), str)):
            return False
        fields = {"init": ("shares", "price"), "buy": ("shares", "price"),
                  "sell": ("shares", "price"), "dividend": ("amount",), "split": ("ratio",)}[row["side"]]
        if any(not isinstance(row.get(key), (int, float)) or isinstance(row[key], bool)
               or row[key] <= 0 or row[key] > 1e12 or not math.isfinite(row[key]) for key in fields):
            return False
        fee = row.get("fee", 0)
        if not isinstance(fee, (int, float)) or isinstance(fee, bool) or fee < 0 or fee > 1e12 or not math.isfinite(fee):
            return False
    return True


def _read_holdings() -> dict:
    data = _strict_json(_holdings_file(), ("positions", "watch"))
    if not _valid_holdings(data):
        raise PortfolioError("corrupt_data", "holdings.json 记录缺少代码；已停止写入并保留原件")
    return data


def _read_trades() -> dict:
    data = _strict_json(_trades_file(), ("items",))
    if not _valid_trades(data):
        raise PortfolioError("corrupt_data", "trades.json 记录格式异常；已停止写入并保留原件")
    return data


def _pending_file() -> Path:
    return settings.CONFIG_DIR / ".portfolio.pending.json"


def _pending_pair() -> tuple[dict, dict] | None:
    pending = _pending_file()
    if not pending.exists() and not pending.is_symlink():
        return None
    data = _strict_json(pending, ())
    if not (_valid_lists(data.get("holdings"), ("positions", "watch"))
            and _valid_lists(data.get("trades"), ("items",))
            and _valid_holdings(data["holdings"]) and _valid_trades(data["trades"])):
        raise PortfolioError("corrupt_data", "持仓事务日志内容异常；已停止写入并保留原件")
    return data["holdings"], data["trades"]


def _recover_pair() -> None:
    pair = _pending_pair()
    if pair is None:
        return
    h, d = pair
    _write(_trades_file(), d)
    _write(_holdings_file(), h)
    _pending_file().unlink()
    _sync_dir(settings.CONFIG_DIR)


def _read_pair() -> tuple[dict, dict]:
    """GET 只读取日志中的一致快照；写入路径才将其恢复到磁盘。"""
    def snapshot() -> tuple[dict, dict]:
        return _pending_pair() or (_read_holdings(), _read_trades())

    with _LOCK:
        if getattr(_WRITE_STATE, "active", False):
            return snapshot()
        lock_file = settings.CONFIG_DIR / ".portfolio.lock"
        if lock_file.is_symlink():
            raise PortfolioError("corrupt_data", "持仓锁文件是符号链接；已停止读取")
        if not lock_file.exists():
            try:
                pair = snapshot()
            except PortfolioError:
                if not lock_file.exists():
                    raise
            else:
                if not lock_file.exists():
                    return pair
        with lock_file.open("rb") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                return snapshot()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def holdings_snapshot() -> dict:
    return _read_pair()[0]


def _write_pair(h: dict, d: dict) -> None:
    """先落恢复日志，再依次提交两文件；异常后下次写请求完成提交。"""
    _write(_pending_file(), {"holdings": h, "trades": d})
    _recover_pair()


@contextlib.contextmanager
def _write_lock():
    """线程与进程间共同串行化 holdings/trades 并完成中断事务的恢复。"""
    with _LOCK:
        lock_file = settings.CONFIG_DIR / ".portfolio.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        if lock_file.is_symlink():
            raise PortfolioError("corrupt_data", "持仓锁文件是符号链接；已停止写入并保留原件")
        with lock_file.open("a+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                _recover_pair()
                _WRITE_STATE.active = True
                try:
                    yield
                finally:
                    _WRITE_STATE.active = False
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: Path, data: dict) -> None:
    """每次写使用唯一临时文件，避免 store 的固定 .tmp 路径并发碰撞。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as f:
            tmp = f.name
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _sync_dir(path.parent)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def _text(value: object, length: int) -> str:
    return str(value if value is not None else "").strip()[:length]


def _number(value: object, field: str, *, allow_zero: bool = False) -> float:
    try:
        if isinstance(value, bool) or value is None or value == "":
            raise ValueError()
        n = float(value)
    except (TypeError, ValueError):
        raise PortfolioError("bad_number", f"{field} 必须为有效数字") from None
    if not math.isfinite(n) or n > 1e12 or n < 0 or (n == 0 and not allow_zero):
        raise PortfolioError("bad_number", f"{field} 必须为不超过 1e12 的{'非负' if allow_zero else '正'}数")
    return n


def identity(code: object, gtimg: object = None) -> dict:
    """仅接受能唯一映射腾讯行情 key 的 A/HK/US 证券身份。"""
    code = _text(code, 24).upper()
    m = re.fullmatch(r"(\d{5})\.HK|([A-Z][A-Z0-9]{0,9})\.US|(\d{6})\.(SH|SZ|BJ)", code)
    if not m:
        raise PortfolioError("bad_code", "代码需为 5 位港股、6 位 A 股或美股 ticker 加市场后缀")
    market_key = "HK" if m[1] else "US" if m[2] else m[4]
    sym = m[1] or m[2] or m[3]
    market, currency, prefix = _MARKETS[market_key]
    key = prefix + sym
    if gtimg is not None and _text(gtimg, 32) != key:
        raise PortfolioError("bad_code", "代码与行情标识不匹配")
    return {"code": code, "gtimg": key, "market": market, "currency": currency}


def _quantity(body: dict) -> tuple[float | None, float | None]:
    shares, cost = body.get("shares"), body.get("cost")
    if shares in ("", None) and cost in ("", None):
        return None, None
    if shares in ("", None) or cost in ("", None):
        raise PortfolioError("bad_quantity", "股数与成本需同时填写，或同时留空")
    return round(_number(shares, "股数"), 6), round(_number(cost, "成本"), 3)


def _sort(items: list[dict]) -> None:
    items.sort(key=lambda t: (t["date"], 0 if t["side"] == "init" else 1, t.get("created_at", "")))


def _new_trade(code: str, name: str, currency: str, date: str, side: str, **fields) -> dict:
    return {"id": "t" + uuid.uuid4().hex[:16], "date": date, "code": code, "name": name,
            "side": side, "currency": currency, "fee": 0, "created_at": _stamp(), **fields}


def _ensure_initial(h: dict, d: dict) -> bool:
    existing = {t.get("code") for t in d["items"]}
    default_date = _text(h.get("updated_at"), 10)
    try:
        datetime.strptime(default_date, "%Y-%m-%d")
    except ValueError:
        default_date = _date()
    changed = False
    for p in h["positions"]:
        if p.get("code") not in existing and p.get("shares") and p.get("cost"):
            trade = _new_trade(p["code"], p.get("name", p["code"]),
                               p.get("currency", "CNY"), default_date, "init",
                               shares=p["shares"], price=p["cost"],
                               reason="期初持仓（由 holdings 快照生成）")
            trade["id"] = "t" + uuid.uuid5(uuid.NAMESPACE_URL, "portfolio-init:" + p["code"]).hex[:16]
            trade["created_at"] = default_date + "T00:00:00+08:00"
            d["items"].append(trade)
            changed = True
    if changed:
        _sort(d["items"])
        d["updated_at"] = _stamp()
    return changed


def _derive(items: list[dict]) -> dict[str, dict]:
    by: dict[str, dict] = {}
    for t in sorted(items, key=lambda x: (x["date"], 0 if x["side"] == "init" else 1, x.get("created_at", ""))):
        if t.get("side") not in _SIDES or any(field not in t for field in ("code", "name", "currency", "date")):
            raise PortfolioError("corrupt_data", "交易流水存在格式异常记录；已停止写入并保留原件")
        s = by.setdefault(t["code"], {"code": t["code"], "name": t["name"], "currency": t["currency"],
                                       "shares": 0., "cost_total": 0., "realized": 0., "dividends": 0.,
                                       "fees": 0., "n": 0, "last_date": None})
        s["n"] += 1
        if t["side"] != "init":
            s["last_date"] = t["date"]
        fee = float(t.get("fee") or 0)
        s["fees"] += fee
        if t["side"] in ("init", "buy"):
            s["shares"] += t["shares"]
            s["cost_total"] += t["shares"] * t["price"] + fee
        elif t["side"] == "sell":
            if t["shares"] > s["shares"] + 1e-8:
                raise PortfolioError("insufficient_shares", f"{t['date']} 卖出股数超过持仓")
            avg = s["cost_total"] / s["shares"] if s["shares"] else 0
            s["realized"] += t["shares"] * (t["price"] - avg) - fee
            s["shares"] -= t["shares"]
            s["cost_total"] -= t["shares"] * avg
            if s["shares"] < 1e-8:
                s["shares"] = 0.
                s["cost_total"] = 0.
        elif t["side"] == "dividend":
            s["dividends"] += t["amount"]
            s["realized"] += t["amount"] - fee
        elif t["side"] == "split":
            s["shares"] *= t["ratio"]
    for s in by.values():
        s["cost"] = s["cost_total"] / s["shares"] if s["shares"] else None
    return by


def _sync(h: dict, states: dict[str, dict]) -> int:
    changed = 0
    for code, state in states.items():
        p = next((x for x in h["positions"] if x["code"] == code), None)
        w = next((x for x in h["watch"] if x["code"] == code), None)
        if state["shares"] > 0:
            if p is None and w is not None:
                h["watch"].remove(w)
                w["type"] = "holding"
                h["positions"].append(w)
                p = w
                changed += 1
            if p is not None:
                shares, cost = round(state["shares"], 6), round(state["cost"], 3)
                if p.get("shares") != shares or p.get("cost") != cost:
                    p["shares"], p["cost"] = shares, cost
                    changed += 1
        elif p is not None:
            h["positions"].remove(p)
            p["type"] = "watch"
            p.pop("shares", None)
            p.pop("cost", None)
            if w is None:
                h["watch"].insert(0, p)
            changed += 1
    if changed:
        h["updated_at"] = _date()
    return changed


def _valid_date(value: object, *, future: bool = False) -> str:
    date = _text(value, 10)
    try:
        if len(date) != 10 or datetime.strptime(date, "%Y-%m-%d").date().isoformat() != date:
            raise ValueError()
    except ValueError:
        raise PortfolioError("bad_date", "日期格式需为 YYYY-MM-DD") from None
    if not future and date > _date():
        raise PortfolioError("bad_date", "交易日期不能晚于今天")
    return date


def mutate_holdings(body: dict) -> dict:
    op, code = _text(body.get("op"), 20), _text(body.get("code"), 24).upper()
    with _write_lock():
        h, d = _read_holdings(), _read_trades()
        p = next((x for x in h["positions"] if x.get("code") == code), None)
        w = next((x for x in h["watch"] if x.get("code") == code), None)
        entry = None
        extra = {}
        if op in ("add_pos", "add_watch"):
            ident = identity(code, body.get("gtimg") if w is None else w.get("gtimg"))
            if p or (op == "add_watch" and w):
                raise PortfolioError("already_exists", "标的已在持仓或关注中")
            if op == "add_pos" and any(t.get("code") == code for t in d["items"]):
                raise PortfolioError("trades_exist", "该股已有历史流水，请通过交易页记录买入，不可重置期初持仓")
            if w:
                entry = w
                h["watch"].remove(w)
            else:
                entry = {**ident, "type": "watch", "name": _text(body.get("name"), 30) or code,
                         "sector": "其他", "note": ""}
                kline = _text(body.get("kline"), 20)
                if ident["market"] == "美股" and re.fullmatch(r"us[A-Z0-9]{1,10}\.(N|AM|OQ)", kline):
                    entry["kline"] = kline
            if "name" in body and _text(body["name"], 30):
                entry["name"] = _text(body["name"], 30)
            if "sector" in body:
                entry["sector"] = _text(body["sector"], 30) or "其他"
            if "note" in body:
                entry["note"] = _text(body["note"], 200)
            if op == "add_pos":
                shares, cost = _quantity(body)
                entry.update(type="holding", shares=shares, cost=cost)
                h["positions"].append(entry)
                if shares is not None:
                    d["items"] = [t for t in d["items"] if not (t.get("code") == code and t.get("side") == "init")]
                    d["items"].append(_new_trade(code, entry["name"], entry["currency"], _date(), "init",
                                                 price=cost, shares=shares, reason="期初持仓（由 holdings 快照生成）"))
                    d["updated_at"] = _stamp()
                extra["counted"] = shares is not None
            else:
                entry["type"] = "watch"
                entry.setdefault("cost", None)
                entry.setdefault("shares", None)
                h["watch"].insert(0, entry)
        elif op in ("update_pos", "update_watch"):
            entry = p if op == "update_pos" else w
            if not entry:
                raise PortfolioError("not_found", "标的不在指定列表")
            identity(code, entry.get("gtimg"))
            changed = None
            if p is not None and ("shares" in body or "cost" in body):
                shares, cost = _quantity(body)
                if shares != p.get("shares") or cost != p.get("cost"):
                    related = [t for t in d["items"] if t.get("code") == code]
                    if any(t.get("side") != "init" for t in related):
                        raise PortfolioError("trades_locked", "该股已有交易流水，请在交易页调整股数与成本")
                    changed = (shares, cost)
            if "name" in body and _text(body["name"], 30):
                entry["name"] = _text(body["name"], 30)
            for key, limit in (("note", 200), ("sector", 30)):
                if key in body:
                    entry[key] = _text(body[key], limit) or ("其他" if key == "sector" else "")
            if changed is not None:
                shares, cost = changed
                p["shares"], p["cost"] = shares, cost
                d["items"] = [t for t in d["items"] if not (t.get("code") == code and t.get("side") == "init")]
                if shares is not None:
                    d["items"].append(_new_trade(code, p["name"], p["currency"], _date(), "init",
                                                 price=cost, shares=shares, reason="期初持仓（由 holdings 快照生成）"))
                d["updated_at"] = _stamp()
                extra["trade_note"] = "已同步更新期初流水"
            elif p and p.get("shares") and not any(t.get("code") == code for t in d["items"]):
                d["items"].append(_new_trade(code, p["name"], p["currency"], _date(), "init",
                                             price=p["cost"], shares=p["shares"], reason="期初持仓（由 holdings 快照生成）"))
                d["updated_at"] = _stamp()
        elif op in ("remove_pos", "remove_watch"):
            entry = p if op == "remove_pos" else w
            if not entry:
                raise PortfolioError("not_found", "标的不在指定列表")
            if p:
                related = [t for t in d["items"] if t.get("code") == code]
                if any(t.get("side") != "init" for t in related):
                    raise PortfolioError("trades_exist", "该股已有真实交易流水；请先卖出清仓，历史记录不会随持仓移除而删除")
                h["positions"].remove(p)
                d["items"] = [t for t in d["items"] if t.get("code") != code]
                if related:
                    d["updated_at"] = _stamp()
                if body.get("to_watch") is True:
                    p["type"] = "watch"
                    p.pop("shares", None)
                    p.pop("cost", None)
                    h["watch"].insert(0, p)
                extra.update(to_watch=body.get("to_watch") is True, trades_removed=len(related))
            else:
                if any(t.get("code") == code for t in d["items"]):
                    raise PortfolioError("trades_exist", "该关注标的仍有历史交易流水；为保留账本，不能直接移除")
                h["watch"].remove(w)
        else:
            raise PortfolioError("bad_op", "不支持的持仓操作")
        _sort(d["items"])
        h["updated_at"] = _date()
        _write_pair(h, d)
        return {"ok": True, "op": op, "code": code, "entry": entry, **extra}


def list_trades() -> dict:
    h, d = _read_pair()
    _ensure_initial(h, d)
    return d


def mutate_trades(body: dict) -> dict:
    with _write_lock():
        h, d = _read_holdings(), _read_trades()
        _ensure_initial(h, d)
        op = body.get("op")
        if op == "add":
            side = body.get("side")
            if side not in _SIDES or side == "init":
                raise PortfolioError("bad_side", "不支持的流水类型")
            ident = identity(body.get("code"))
            stock = next((p for p in h["positions"] + h["watch"] if p.get("code") == ident["code"]), None)
            if stock is None or stock.get("gtimg") != ident["gtimg"]:
                raise PortfolioError("unknown_code", "请先把标的加入持仓或关注")
            date = _valid_date(body.get("date") or _date())
            t = _new_trade(stock["code"], stock.get("name", stock["code"]), stock["currency"], date, side,
                           reason=_text(body.get("reason"), 1000))
            t["fee"] = _number(body.get("fee") or 0, "费用", allow_zero=True)
            if body.get("fx") not in ("", None):
                t["fx"] = _number(body["fx"], "汇率")
            if side in ("buy", "sell"):
                t["price"] = _number(body.get("price"), "成交价")
                t["shares"] = _number(body.get("shares"), "股数")
                if side == "sell":
                    t["reason_cat"] = _text(body.get("reason_cat"), 20)
                    if t["reason_cat"] not in _SELL_REASONS or len(t["reason"]) < 10:
                        raise PortfolioError("sell_reason_required", "卖出需选择原因类别，且原因至少 10 个字")
            elif side == "dividend":
                t["amount"] = _number(body.get("amount"), "分红金额")
            else:
                t["ratio"] = _number(body.get("ratio"), "拆股比例")
                if t["ratio"] == 1:
                    raise PortfolioError("bad_ratio", "拆股比例不能为 1")
            d["items"].append(t)
        elif op in ("update", "delete"):
            t = next((x for x in d["items"] if x.get("id") == body.get("id")), None)
            if t is None:
                raise PortfolioError("not_found", "未找到交易记录")
            if op == "delete":
                if t["side"] == "init":
                    raise PortfolioError("immutable_init", "期初持仓不能直接删除，请在持仓页修改或移除")
                d["items"].remove(t)
            else:
                if "date" in body:
                    t["date"] = _valid_date(body["date"])
                if t["side"] == "init" and any(key in body for key in ("price", "shares", "fee", "amount", "ratio")):
                    raise PortfolioError("immutable_init", "请在持仓页修改期初价格和股数")
                for key in ("price", "shares", "amount", "fee", "fx", "ratio"):
                    if key in body and key in t:
                        t[key] = _number(body[key], key, allow_zero=key == "fee")
                if "reason" in body:
                    t["reason"] = _text(body["reason"], 1000)
                if t["side"] == "sell" and (len(t.get("reason", "")) < 10 or t.get("reason_cat") not in _SELL_REASONS):
                    raise PortfolioError("sell_reason_required", "卖出需选择原因类别，且原因至少 10 个字")
                if t["side"] == "split" and t["ratio"] == 1:
                    raise PortfolioError("bad_ratio", "拆股比例不能为 1")
                t["updated_at"] = _stamp()
        else:
            raise PortfolioError("bad_op", "不支持的交易操作")
        _sort(d["items"])
        by = _derive(d["items"])
        if op == "delete" and t["code"] not in by:
            stock = next((p for p in h["positions"] if p.get("code") == t["code"]), None)
            if stock:
                h["positions"].remove(stock)
                stock["type"] = "watch"
                stock.pop("shares", None)
                stock.pop("cost", None)
                h["watch"].insert(0, stock)
                h["updated_at"] = _date()
                orphaned = 1
            else:
                orphaned = 0
        else:
            orphaned = 0
        synced = orphaned + _sync(h, by)
        d["updated_at"] = _stamp()
        _write_pair(h, d)
        # 流水变了，当天的归因缓存作废（下次读取重算）
        try:
            _f = settings.PANEL_OUT_DIR / "attribution_cache.json"
            if _f.exists():
                _f.unlink()
        except OSError:
            pass
        return {"ok": True, "synced": synced}


def trade_snapshot() -> tuple[dict, dict, dict]:
    h, d = _read_pair()
    _ensure_initial(h, d)
    return h, d, _derive(d["items"])
