"""gtimg 前复权日 K 与汇率（对齐 8899 server.js 的 fetchKline/fetchFx 口径）。

- 数据源：web.ifzq.gtimg.cn fqkline（前复权日 K）与 qt.gtimg.cn 汇率，均为公开行情接口；
- 磁盘缓存 data/cache/kline/，30 分钟内复用；美股需交易所后缀（NYSE=.N，其余默认 .OQ），
  holdings 条目可用 kline 字段覆盖；
- 汇率 5 分钟缓存，失败回退组合红线里的 fx_fallback。
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import settings

_TZ = timezone(timedelta(hours=8))
def _cache_dir() -> Path:
    return settings.DATA_DIR / "cache" / "kline"
_KLINE_TTL_SEC = 1800
_FX_TTL_SEC = 300
_UA = {"User-Agent": "Mozilla/5.0"}

# 与 8899 保持一致：NYSE 上市的美股用 .N 后缀
_US_NYSE = {s.strip().upper() for s in (
    "TSM,BABA,NIO,XPEV,JPM,V,MA,KO,DIS,TME,YUMC,BRK.B").split(",") if s.strip()}

_mem: dict[str, tuple[float, int, list[dict]]] = {}
_fx_mem: tuple[float, dict] | None = None


def bj_now() -> datetime:
    return datetime.now(_TZ)


def bj_date() -> str:
    return bj_now().date().isoformat()


def bj_full() -> str:
    return bj_now().strftime("%Y-%m-%d %H:%M:%S")


def shift_date(ds: str, days: int) -> str:
    d = datetime.strptime(ds[:10], "%Y-%m-%d") + timedelta(days=days)
    return d.date().isoformat()


def iso_week_of(ds: str) -> dict:
    """8899 口径：{id: 2026-W39, monday, sunday}（周一起算）。"""
    d = datetime.strptime(ds[:10], "%Y-%m-%d").date()
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    iso = monday.isocalendar()
    return {"id": f"{iso.year}-W{iso.week:02d}", "monday": monday.isoformat(),
            "sunday": sunday.isoformat()}


def week_from_id(wid: str) -> dict | None:
    m = re.fullmatch(r"(\d{4})-W(\d{2})", wid or "")
    if not m:
        return None
    try:
        monday = datetime.strptime(f"{m[1]}-W{int(m[2]):02d}-1", "%G-W%V-%u").date()
    except ValueError:
        return None
    return {"id": wid, "monday": monday.isoformat(),
            "sunday": (monday + timedelta(days=6)).isoformat()}


def default_week() -> dict:
    """周五~周日默认本周，周一~周四默认上周（与 8899 一致）。"""
    now = bj_now()
    cur = iso_week_of(now.date().isoformat())
    dow = now.weekday() + 1
    if dow >= 5 or dow == 7:
        return cur
    return iso_week_of(shift_date(cur["monday"], -7))


def kline_sym_of(gtimg: str, entry: dict | None = None) -> str:
    if entry and entry.get("kline"):
        return str(entry["kline"])
    if not re.match(r"(?i)^us", gtimg or ""):
        return gtimg
    t = gtimg[2:].upper()
    if "." in t:
        return gtimg
    return "us" + t + (".N" if t in _US_NYSE else ".OQ")


def clean_bars(bars: list[dict]) -> list[dict]:
    """清洗：美股接口偶尔在头部混入旧脏数据，只保留日期连续的尾段（对齐 SIG.cleanBars）。"""
    rows = [x for x in (bars or [])
            if x and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(x.get("d") or ""))
            and isinstance(x.get("c"), (int, float)) and x["c"] > 0]
    start = 0
    for i in range(1, len(rows)):
        gap = (datetime.strptime(rows[i]["d"], "%Y-%m-%d")
               - datetime.strptime(rows[i - 1]["d"], "%Y-%m-%d")).days
        if gap > 30:
            start = i
    return rows[start:]


def fetch_kline(sym: str, n: int = 260) -> list[dict]:
    """前复权日 K，返回 [{d,o,c,h,l}] 升序；失败抛异常（调用方按标的降级）。"""
    n = min(640, max(20, int(n or 260)))
    now = time.time()
    hit = _mem.get(sym)
    if hit and now - hit[0] < _KLINE_TTL_SEC and hit[1] >= n:
        return hit[2]
    _cache_dir().mkdir(parents=True, exist_ok=True)
    disk = _cache_dir() / f"{re.sub(r'[^0-9A-Za-z.]', '_', sym)}.json"
    if disk.exists() and now - disk.stat().st_mtime < _KLINE_TTL_SEC:
        try:
            cached = json.loads(disk.read_text(encoding="utf-8"))
            bars = cached.get("bars") or []
            if len(bars) >= 2 and cached.get("n", 0) >= n:
                _mem[sym] = (now, cached["n"], bars)
                return bars
        except (OSError, ValueError):
            pass
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
           + urllib.parse.quote(sym) + f",day,,,{n},qfq")
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    node = (payload.get("data") or {}).get(sym) or {}
    raw = node.get("qfqday") or node.get("day")
    if not isinstance(raw, list) or len(raw) < 2:
        raise RuntimeError(f"K 线为空（{sym}）")
    bars = []
    for x in raw:
        try:
            bars.append({"d": str(x[0]), "o": float(x[1]), "c": float(x[2]),
                         "h": float(x[3]), "l": float(x[4])})
        except (IndexError, TypeError, ValueError):
            continue
    bars = clean_bars(bars)
    if len(bars) < 2:
        raise RuntimeError(f"K 线为空（{sym}）")
    _mem[sym] = (now, n, bars)
    try:
        disk.write_text(json.dumps({"n": n, "at": now, "bars": bars}), encoding="utf-8")
    except OSError:
        pass
    return bars


def _fx_fallback() -> dict:
    try:
        rules = json.loads((settings.CONFIG_DIR / "portfolio_rules.json").read_text(encoding="utf-8"))
        fb = rules.get("fx_fallback")
        if isinstance(fb, dict):
            return {k: float(v) for k, v in fb.items() if isinstance(v, (int, float))}
    except (OSError, ValueError, TypeError):
        pass
    return {"CNY": 1.0, "HKD": 0.86, "USD": 6.72}


def fetch_fx() -> dict:
    """{rates: {HKD, USD, CNY}, source, at}；5 分钟缓存，失败回退兜底汇率。"""
    global _fx_mem
    now = time.time()
    if _fx_mem and now - _fx_mem[0] < _FX_TTL_SEC:
        return _fx_mem[1]
    data: dict[str, Any] = {"rates": _fx_fallback(), "source": "fallback", "at": None}
    try:
        req = urllib.request.Request("https://qt.gtimg.cn/q=whHKDCNY,whUSDCNY", headers=_UA)
        with urllib.request.urlopen(req, timeout=6) as resp:
            text = resp.read().decode("gbk", errors="replace")
        got = 0
        at = None
        for m in re.finditer(r'v_wh([A-Z]{3})CNY="([^"]*)"', text):
            f = m[2].split("~")
            try:
                rate = float(f[3])
            except (IndexError, ValueError):
                continue
            if rate > 0:
                data["rates"][m[1]] = rate
                got += 1
                if len(f) > 5 and re.fullmatch(r"\d{14}", f[5] or ""):
                    ts = f[5]
                    at = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}"
        if got:
            data["source"], data["at"] = "qt.gtimg.cn", at
    except Exception:
        pass
    data["rates"]["CNY"] = 1.0
    _fx_mem = (now, data)
    return data
