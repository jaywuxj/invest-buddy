"""选股周更流水线 + 长期跟踪账本 + 接口（2026-11-03）。

周更（周六 10:00，screener-weekly）：
  ① 两个市场实时快照 → 月 K → screener.score()（与回测同一函数）
  ② 剔除硬伤 / 已持仓 / 已关注 / 已忽略，取综合分前 deep 名做深度复核：估值三把尺子（valuation.view）+ 机构评级
  ③ 估值「偏贵 / 明显偏贵」的不进精选（列出原因）；每个市场取前 picks 名为本周精选
  ④ 模型（screener-reasons）只把程序给的事实写成推荐理由 / 风险 / 证伪条件，开启数字对账
  ⑤ 与上周对比（新进 / 连续 N 周 / 退出及原因），写入跟踪账本：每只「首次入选」记一次，
     按入选价跟踪 20 / 60 / 120 / 250 个交易日后相对指数的超额收益（买入持有口径，不因退出而停止）
所有数字由程序计算；模型输出失败时精选照常发布，只是没有理由文字。
"""
from __future__ import annotations

import asyncio
import json
import math
import statistics
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import kline as kl
from . import portfolio, settings
from . import screener as S
from .providers import westock
from .store import atomic_write_json, log_run, read_json

HORIZONS = (20, 60, 120, 250)
_LOCK = threading.Lock()
STATE: dict[str, Any] = {"running": False, "stage": "", "started_at": None, "error": None, "bt_running": False}


def _out_file() -> Path:
    return settings.PANEL_OUT_DIR / "screener.json"


def _ledger_file() -> Path:
    return S._dir() / "ledger.json"


def _state_file() -> Path:
    return S._dir() / "state.json"


def _strict(path: Path, default: dict) -> dict:
    """账本 / 忽略列表属于长期记录：损坏或符号链接直接报错，绝不覆盖。"""
    if not path.exists():
        return default
    if path.is_symlink():
        raise RuntimeError(f"{path.name} 是符号链接，已停止读写")
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise RuntimeError(f"{path.name} 无法解析，已停止写入并保留原件") from None
    if not isinstance(d, dict):
        raise RuntimeError(f"{path.name} 结构异常")
    return d


def load_ledger() -> dict:
    return _strict(_ledger_file(), {"episodes": [], "runs": []})


def load_state() -> dict:
    return _strict(_state_file(), {"ignored": {}})


def _stage(s: str) -> None:
    STATE["stage"] = s


def _held() -> dict[str, str]:
    h = portfolio.load_holdings()
    out = {p["code"]: "持仓" for p in h.get("positions", []) if p.get("code")}
    out.update({p["code"]: "关注" for p in h.get("watch", []) if p.get("code")})
    return out


KEEP = ("code", "ws", "name", "mk", "mv", "px", "px_live", "score", "rank", "f", "tags", "flags", "industry",
        "pe", "pb", "ps", "dy", "roe", "npm", "gpm", "revg", "npg", "mom", "hiprox", "vol")


def _compact(r: dict) -> dict:
    out = {k: r.get(k) for k in KEEP}
    for k in ("pe", "pb", "ps", "dy", "roe", "npm", "gpm", "revg", "npg", "mom", "hiprox", "vol", "px"):
        if isinstance(out.get(k), float):
            out[k] = round(out[k], 2)
    out["kline_sym"] = (S._km().get(r.get("ws") or "") or {}).get("sym")
    if out.get("px_live"):
        out["px"] = out["px_live"]
    out.pop("px_live", None)
    return out


def _valuation(ws: str) -> dict | None:
    from . import valuation
    try:
        d = valuation.view(ws)
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:80]}"}
    return valuation.compact(d)


def _rating(wss: list[str]) -> dict:
    out = {}
    for i in range(0, len(wss), 6):
        try:
            out.update(westock.rating(wss[i:i + 6]))
        except Exception:
            continue
    return out


def _facts(p: dict, mk: str) -> dict:
    """给模型的事实包：只放程序算好的数字。"""
    v = p.get("valuation") or {}
    a = p.get("analyst") or {}
    m = {k: p.get(k) for k in ("pe", "pb", "dy", "roe", "npm", "gpm", "revg", "npg") if p.get(k) is not None}
    if p.get("mom") is not None:
        m["近一年涨跌（跳过最近1个月，%）"] = round(p["mom"], 1)
    if p.get("hiprox") is not None:
        m["现价相当于12个月高点的（%）"] = round(p["hiprox"], 1)
    return {"code": p["code"], "name": p["name"], "market": S.MARKETS[mk]["name"],
            "industry": p.get("industry"), "company_intro（来自公司简况，可用于描述业务）": p.get("intro"),
            "metrics_note": "pe=滚动市盈率 pb=市净率 dy=股息率% roe=净资产收益率% npm=净利率% gpm=毛利率% revg=营收同比% npg=归母净利同比%",
            "metrics": m, "factor_pct": {S.FACTOR_CN[k]: v2 for k, v2 in (p.get("f") or {}).items() if v2 is not None},
            "score": p.get("score"), "rank_in_pool": p.get("rank"), "tags": p.get("tags"),
            "red_flags": [x["k"] + "：" + x["why"] for x in p.get("flags") or []],
            "valuation": {k: v.get(k) for k in ("verdict", "confidence", "warn") if v.get(k)} | (
                {"历史分位%": (v.get("hist") or {}).get("rank"),
                 "反向DCF安全边际%": round(v["dcf"]["mos"] * 100) if (v.get("dcf") or {}).get("mos") is not None and abs(v["dcf"]["mos"]) <= 0.8 else "不适用（结果超出合理范围，常见于银行等现金流口径不适用的行业）",
                 "现价隐含年增速%": round(v["dcf"]["implied_g"] * 100, 1) if (v.get("dcf") or {}).get("implied_g") is not None else None}
                if v.get("ok") else {"说明": v.get("reason") or "估值数据不足"}),
            "analyst": {"机构数": a.get("count"), "买入占比%": a.get("buy_pct"), "目标价空间%": a.get("upside")} if a else {}}


async def _reasons(picks: list[dict], mk: str) -> dict[str, dict]:
    from . import quality
    from .jobs.common import run_skill_pipeline
    from .skills import registry
    if not picks:
        return {}
    skill = registry.get("screener-reasons")
    items = [_facts(p, mk) for p in picks[:7]]
    try:
        res = await run_skill_pipeline(
            skill, {"market": S.MARKETS[mk]["name"], "n": len(items), "today": kl.bj_date(),
                    "items_json": json.dumps(items, ensure_ascii=False, indent=1)},
            settings.PANEL_OUT_DIR / "_stage" / f"screener_reasons_{mk}.json",
            source_numbers=quality.numbers_from_digest(items))
    except Exception as exc:
        log_run("screener-weekly", "partial", f"{mk} 推荐理由生成失败：{str(exc)[:160]}")
        return {}
    return {str(x.get("code")): x for x in res.get("items") or [] if isinstance(x, dict)}


def _market(mk: str, held: dict, ignored: dict, c: dict) -> dict:
    """阻塞部分（westock 限频）：快照 → 月 K → 打分 → 深度复核。"""
    _stage(f"{S.MARKETS[mk]['name']}：拉取候选池")
    snap = S.snapshot(mk, None, c["min_mv"][mk])
    _stage(f"{S.MARKETS[mk]['name']}：{len(snap)} 只，拉取月 K")
    bars = S.load_month_bars(list(snap) + [S.MARKETS[mk]["bench"]], max_age_h=20)
    rows = S.score(S.build_rows(snap, bars, kl.bj_date()), mk, c["weights"])
    scored = [r for r in rows if r.get("score") is not None]
    for r in scored:
        r["held"] = held.get(r["code"])
        r["ignored"] = r["code"] in ignored
    clean = [r for r in scored if not any(x["level"] == "hard" for x in r["flags"])]
    deep = [r for r in clean if not r["held"] and not r["ignored"]][:c["deep"]]
    _stage(f"{S.MARKETS[mk]['name']}：估值复核 {len(deep)} 只")
    rat = _rating([r["ws"] for r in deep])
    prof: dict = {}
    for i in range(0, len(deep), 6):
        try:
            prof.update(westock.profile([r["ws"] for r in deep[i:i + 6]]))
        except Exception:
            pass
    for r in deep:
        pf = prof.get(r["ws"]) or {}
        r["industry"] = pf.get("industry") or pf.get("sector")
        r["intro"] = pf.get("intro")
    for r in deep:
        r["valuation"] = _valuation(r["ws"])
        cur = (rat.get(r["ws"]) or {}).get("cur") or {}
        px = r.get("px_live") or r.get("px")
        if cur:
            up = (cur["target_avg"] / px - 1) * 100 if cur.get("target_avg") and px else None
            r["analyst"] = {"count": cur.get("count"), "buy_pct": cur.get("buy_pct"),
                            "target_avg": cur.get("target_avg"), "upside": round(up, 1) if up is not None else None}
    picks, dropped, per_ind = [], [], {}
    for r in deep:
        v = r.get("valuation") or {}
        if v.get("ok") and v.get("verdict") in ("偏贵", "明显偏贵"):
            dropped.append({**_compact(r), "why": f"估值复核：{v['verdict']}（三把尺子），暂不推荐"})
            continue
        dcf = v.get("dcf") or {}
        mos = dcf.get("mos")
        # 「严重低估」以估值引擎的结论为准；反向 DCF 对银行等不适用时会给出离谱的安全边际，不能单独作依据
        if v.get("ok") and v.get("confidence") != "低" and (
                v.get("verdict") == "明显便宜" or (v.get("verdict") == "偏便宜" and mos is not None and 0.3 <= mos <= 0.8)):
            r["tags"] = ["严重低估"] + [t for t in r["tags"] if t != "严重低估"]
        if len(picks) >= c["picks"]:
            continue
        ind = r.get("industry") or "未知行业"
        if ind != "未知行业" and per_ind.get(ind, 0) >= 2:
            dropped.append({**_compact(r), "why": f"行业分散：「{ind}」已入选 2 只"})
            continue
        per_ind[ind] = per_ind.get(ind, 0) + 1
        picks.append(r)
    return {"name": S.MARKETS[mk]["name"], "bench_name": S.MARKETS[mk]["bench_name"], "pool_n": len(snap),
            "scored_n": len(scored), "hard_n": len(scored) - len(clean),
            "universe": f"市值 ≥ {int(c['min_mv'][mk] / 1e8)} 亿{S.MARKETS[mk]['cur']}",
            "picks_raw": picks, "dropped": dropped, "bench_sym": S.MARKETS[mk]["bench"],
            "candidates": [{**_compact(r), "held": r["held"], "ignored": r["ignored"]} for r in scored[:60]]}


def _pick_out(p: dict, reasons: dict) -> dict:
    return {**_compact(p), "valuation": p.get("valuation"), "analyst": p.get("analyst"),
            "reason": reasons.get(p["code"])}


# ---------------------------------------------------------------- 跟踪账本

def _daily(ws: str) -> list[dict]:
    sym = ws if not ws.startswith("us") or "." in ws else ((S._km().get(ws) or {}).get("sym") or kl.kline_sym_of(ws))
    try:
        return kl.fetch_kline(sym, 640)
    except Exception:
        return []


def _at_or_after(bars: list[dict], d: str) -> int:
    for i, b in enumerate(bars):
        if b["d"] >= d:
            return i
    return -1


def update_perf(led: dict) -> None:
    """按入选日之后的交易日数计算 20/60/120/250 日收益与相对指数超额（买入持有口径）。"""
    bench_cache: dict[str, list[dict]] = {}
    for e in led["episodes"]:
        bars = _daily(e["ws"])
        bsym = S.MARKETS[e["mk"]]["bench"]
        if bsym not in bench_cache:
            bench_cache[bsym] = _daily(bsym)
        bb = bench_cache[bsym]
        i, j = _at_or_after(bars, e["entry_date"]), _at_or_after(bb, e["entry_date"])
        if i < 0 or j < 0:
            continue
        p0, b0 = e.get("entry_px") or bars[i]["c"], bb[j]["c"]
        perf = {}
        for h in HORIZONS:
            if i + h < len(bars) and j + h < len(bb):
                r = (bars[i + h]["c"] / p0 - 1) * 100
                br = (bb[j + h]["c"] / b0 - 1) * 100
                perf[f"d{h}"] = {"ret": round(r, 2), "bench": round(br, 2), "ex": round(r - br, 2), "date": bars[i + h]["d"]}
        r = (bars[-1]["c"] / p0 - 1) * 100
        br = (bb[-1]["c"] / b0 - 1) * 100
        e["perf"] = perf
        e["now"] = {"ret": round(r, 2), "bench": round(br, 2), "ex": round(r - br, 2), "date": bars[-1]["d"],
                    "days": len(bars) - 1 - i}


def ledger_stats(led: dict) -> dict:
    """按持有期汇总：样本 < 10 只写「样本太少」；之后看超额均值的 t 值，如实给结论。"""
    out = {}
    for h in HORIZONS:
        xs = [e["perf"][f"d{h}"]["ex"] for e in led["episodes"] if f"d{h}" in (e.get("perf") or {})]
        n = len(xs)
        if n == 0:
            out[f"d{h}"] = {"n": 0, "verdict": "尚无到期样本"}
            continue
        m = statistics.fmean(xs)
        t = m / statistics.stdev(xs) * math.sqrt(n) if n >= 3 and statistics.stdev(xs) else None
        v = ("样本太少" if n < 10 else "跑赢指数（显著）" if (t or 0) >= 2 else "跑输指数（显著）" if (t or 0) <= -2
             else "与指数无显著差异")
        out[f"d{h}"] = {"n": n, "ex": round(m, 2), "hit": round(sum(1 for x in xs if x > 0) / n * 100), "t": round(t, 2) if t else None,
                        "verdict": v}
    act = [e for e in led["episodes"] if e.get("now")]
    if act:
        out["now"] = {"n": len(act), "ex": round(statistics.fmean(e["now"]["ex"] for e in act), 2),
                      "hit": round(sum(1 for e in act if e["now"]["ex"] > 0) / len(act) * 100)}
    return out


def _update_ledger(markets: dict, today: str) -> dict:
    with _LOCK:
        led = load_ledger()
        active = {e["code"]: e for e in led["episodes"] if e.get("status") == "active"}
        picked = set()
        for mk, m in markets.items():
            for p in m.get("picks", []):
                picked.add(p["code"])
                e = active.get(p["code"])
                if e:
                    if e.get("last_seen") != today:
                        e["weeks"] = int(e.get("weeks") or 1) + 1
                    e["last_seen"] = today
                    e["score"] = p.get("score")
                    p["status"] = {"kind": "continuing", "weeks": e["weeks"], "since": e["entry_date"]}
                else:
                    led["episodes"].append({"id": f"{p['code']}@{today}", "code": p["code"], "ws": p["ws"], "name": p["name"], "mk": mk,
                                            "entry_date": today, "entry_px": p.get("px"), "entry_score": p.get("score"),
                                            "score": p.get("score"), "tags": p.get("tags"), "weeks": 1, "last_seen": today,
                                            "status": "active"})
                    p["status"] = {"kind": "new", "weeks": 1, "since": today}
        exits = []
        for code, e in active.items():
            if code not in picked and e["mk"] in markets:   # 某市场本周取数失败时不误判退出
                e["status"], e["exit_date"] = "exited", today
                exits.append(e)
        for mk, m in markets.items():
            reasons = {d["code"]: d["why"] for d in m.get("dropped", [])}
            cand = {c["code"]: c for c in m.get("candidates", [])}
            m["exited"] = []
            for e in exits:
                if e["mk"] != mk:
                    continue
                c = cand.get(e["code"])
                why = reasons.get(e["code"]) or (
                    "已加入持仓 / 关注" if c and c.get("held") else
                    f"出现硬伤：{'、'.join(x['k'] for x in c['flags'] if x['level'] == 'hard')}" if c and any(x["level"] == "hard" for x in c["flags"]) else
                    f"综合分降至 {c['score']}（第 {c['rank']} 名），被更好的标的挤出" if c else "不再满足市值门槛或数据缺失")
                e["exit_reason"] = why
                m["exited"].append({"code": e["code"], "name": e["name"], "weeks": e.get("weeks"), "why": why})
        led["runs"] = (led.get("runs") or [])[-104:] + [{"date": today, "picks": sorted(picked)}]
        _stage("更新跟踪账本")
        update_perf(led)
        led["updated_at"] = S.bj_now()
        atomic_write_json(_ledger_file(), led)
        return led


async def run_weekly() -> dict:
    if STATE["running"]:
        return {"ok": False, "error": "running"}
    if not westock.available():
        return {"ok": False, "error": "westock_unavailable"}
    STATE.update(running=True, started_at=S.bj_now(), error=None, stage="准备")
    t0 = time.time()
    try:
        load_ledger()                                  # 账本损坏时先停，不做任何写入
        st = load_state()
        held, c, today = _held(), S.cfg(), kl.bj_date()
        markets: dict[str, dict] = {}
        errors = []
        for mk in ("hk", "us"):
            try:
                markets[mk] = await asyncio.to_thread(_market, mk, held, st.get("ignored") or {}, c)
            except Exception as exc:
                errors.append(f"{S.MARKETS[mk]['name']}：{str(exc)[:160]}")
        if not markets:
            raise RuntimeError("；".join(errors) or "两个市场都失败")
        for mk, m in markets.items():
            _stage(f"{m['name']}：生成推荐理由")
            rs = await _reasons(m["picks_raw"], mk)
            m["picks"] = [_pick_out(p, rs) for p in m.pop("picks_raw")]
        prev = read_json(_out_file(), {}) or {}
        led = await asyncio.to_thread(_update_ledger, markets, today)
        out = {"generated_at": S.bj_now(), "date": today, "weights": c["weights"], "tag_def": S.TAG_DEF,
               "factor_cn": S.FACTOR_CN, "markets": markets, "errors": errors, "prev_date": prev.get("date"),
               "took_s": round(time.time() - t0), "ledger": ledger_stats(led)}
        atomic_write_json(_out_file(), out)
        n = sum(len(m["picks"]) for m in markets.values())
        log_run("screener-weekly", "ok" if not errors else "partial", f"精选 {n} 只，用时 {out['took_s']}s" + ("；" + "；".join(errors) if errors else ""))
        return {"ok": True, "picks": n, "errors": errors}
    except Exception as exc:
        STATE["error"] = str(exc)[:200]
        log_run("screener-weekly", "error", str(exc)[:200])
        raise
    finally:
        STATE.update(running=False, stage="")


def set_ignore(code: str, on: bool) -> dict:
    with _LOCK:
        st = load_state()
        ig = st.setdefault("ignored", {})
        if on:
            ig[code] = S.bj_now()[:10]
        else:
            ig.pop(code, None)
        atomic_write_json(_state_file(), st)
        return ig


# ---------------------------------------------------------------- 接口

def register_screener_routes(app: FastAPI) -> None:
    from . import screener_bt

    @app.get("/api/screener")
    async def get_screener():
        d = read_json(_out_file(), {}) or {}
        bt = screener_bt.read()
        try:
            ig = load_state().get("ignored") or {}
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": "state_corrupt", "message": str(exc)}, status_code=500)
        held = _held()
        for m in (d.get("markets") or {}).values():          # 持仓 / 关注 / 忽略状态以当前为准
            for x in (m.get("picks") or []) + (m.get("candidates") or []):
                x["held"] = held.get(x["code"])
                x["ignored"] = x["code"] in ig
        return {"ok": True, "data": d or None, "state": {k: STATE[k] for k in ("running", "stage", "started_at", "error", "bt_running")},
                "backtest": {mk: {"summary": v.get("summary"), "factors": v.get("factors"), "tags": v.get("tags"),
                                  "series": v.get("series"), "universe": v.get("universe"), "bench_name": v.get("bench_name")}
                             for mk, v in (bt.get("markets") or {}).items()} if bt else None,
                "backtest_meta": {k: bt.get(k) for k in ("generated_at", "from", "to", "top_n", "took_s")} if bt else None,
                "westock": westock.available()}

    @app.post("/api/screener/run")
    async def post_screener_run():
        if STATE["running"]:
            return {"ok": True, "running": True, "stage": STATE["stage"]}
        if not westock.available():
            return JSONResponse({"ok": False, "error": "westock_unavailable", "message": "未安装 westock CLI，选股无法取数"}, status_code=400)

        async def bg():
            try:
                await run_weekly()
            except Exception:
                pass
        asyncio.create_task(bg())
        await asyncio.sleep(0.05)
        return {"ok": True, "running": True, "stage": STATE["stage"]}

    @app.post("/api/screener/backtest/run")
    async def post_bt_run():
        if STATE["bt_running"]:
            return {"ok": True, "running": True}
        STATE["bt_running"] = True

        def work():
            try:
                screener_bt.run(log=lambda m: log_run("screener-backtest", "info", m))
                log_run("screener-backtest", "ok", "回测完成")
            except Exception as exc:
                log_run("screener-backtest", "error", str(exc)[:200])
            finally:
                STATE["bt_running"] = False
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "running": True}

    @app.get("/api/screener/ledger")
    async def get_ledger():
        try:
            led = load_ledger()
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": "ledger_corrupt", "message": str(exc)}, status_code=500)
        return {"ok": True, "episodes": list(reversed(led["episodes"])), "runs": led.get("runs", [])[-12:],
                "stats": ledger_stats(led), "updated_at": led.get("updated_at")}

    @app.post("/api/screener/ledger/refresh")
    async def post_ledger_refresh():
        def work():
            with _LOCK:
                led = load_ledger()
                update_perf(led)
                led["updated_at"] = S.bj_now()
                atomic_write_json(_ledger_file(), led)
                return ledger_stats(led)
        try:
            return {"ok": True, "stats": await asyncio.to_thread(work)}
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": "ledger_corrupt", "message": str(exc)}, status_code=500)

    @app.post("/api/screener/ignore")
    async def post_ignore(body: dict[str, Any]):
        code = str(body.get("code") or "")
        if not (code.endswith(".HK") or code.endswith(".US")) or len(code) > 16:
            return JSONResponse({"ok": False, "error": "bad_code"}, status_code=400)
        try:
            return {"ok": True, "ignored": set_ignore(code, body.get("on", True) is not False)}
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": "state_corrupt", "message": str(exc)}, status_code=500)
