"""个股新闻采集：多源 + 相关性过滤 + 同类合并 + 跨媒体去重。

为什么不用 akshare.stock_news_em：它按「证券代码」搜、pageSize 写死 10，
港股结果几乎被「证券时报·数据宝」的每日回购快讯占满（实测腾讯 10/10 条都是回购）。

来源：
- 东方财富资讯搜索（直调 search-api-web，按公司简称/别名搜，每词 50 条）——港美股中文资讯主力
- Finnhub company-news（美股英文资讯，需 FINNHUB_API_KEY，按 related 字段 + 名称过滤）

多样性处理（程序规则，零 LLM）：
1. 相关性：标题必须包含公司别名，否则丢弃（过滤「港股收评」类泛市场稿）
2. 同类合并：去掉数字后的「标题模板」相同（如「X月X日回购X万股，耗资X亿港元」）→ 只留最新一条，
   标注合并条数；例行快讯族（回购/资金流/持股变动/行情播报）同族同日 1 条、全期最多 ROUTINE_CAP 条
3. 跨媒体去重：标题字符 bigram 相似度 ≥ 0.55 视为同一事件，只留一条
4. 单日限额 PER_DAY_CAP：热点当天几十家媒体改写同一事件时不刷屏，保留时间跨度
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from typing import Any

import httpx

from .settings import env_key

PER_STOCK_CAP = 12
PER_DAY_CAP = 3      # 单股单日最多 3 条（窗口已收窄为近 3 天）
ROUTINE_CAP = 2
_TAG_RE = re.compile(r"<[^>]+>")
_NUM_RE = re.compile(r"[\d.,，%％]+")

# 例行快讯族（自动化机器稿）：每族每股最多 ROUTINE_CAP 条
ROUTINE_PATTERNS = {
    "回购": re.compile(r"回购"),
    "资金流": re.compile(r"南向资金|港股通|北向资金|主力资金|净买入|净卖出|资金流"),
    "持股变动": re.compile(r"持股(量|比例)|权益披露|披露.*权益"),
    "行情播报": re.compile(r"涨超|跌超|涨逾|跌逾|股价|收涨|收跌|盘中|拉升|跳水|异动"),
}

# 名称 → 搜索别名的规则化推导失效时的手工补充（用户可在 holdings.json 用 news_keywords 覆盖）
ALIAS_OVERRIDE = {
    "GOOGL.US": ["谷歌", "Alphabet"],
    "META.US": ["Meta"],
    "NVDA.US": ["英伟达"],
    "TSM.US": ["台积电"],
    "AMD.US": ["AMD"],
    "MSFT.US": ["微软"],
    "02015.HK": ["理想汽车"],
    "09660.HK": ["地平线"],
    "00941.HK": ["中国移动"],
    "01211.HK": ["比亚迪"],
    "02513.HK": ["智谱"],
}
EN_ALIAS = {
    "GOOGL.US": ["Google", "Alphabet"],
    "META.US": ["Meta"],
    "NVDA.US": ["Nvidia", "NVIDIA"],
    "TSM.US": ["TSMC", "Taiwan Semiconductor"],
    "AMD.US": ["AMD"],
    "MSFT.US": ["Microsoft"],
}


def aliases(stock: dict) -> list[str]:
    if stock.get("news_keywords"):
        return list(stock["news_keywords"])
    code = stock["code"]
    if code in ALIAS_OVERRIDE:
        return ALIAS_OVERRIDE[code]
    name = re.sub(r"-[A-Z]$", "", stock.get("name", "")).strip()
    short = re.sub(r"(控股|集团|股份|有限公司)$", "", name)
    out = [short] if short else []
    if name and name != short:
        out.append(name)
    return out or [code.split(".")[0]]


def _clean(s: Any) -> str:
    return _TAG_RE.sub("", str(s or "")).replace("\u3000", " ").strip()


async def _em_search(client: httpx.AsyncClient, keyword: str, size: int = 50) -> list[dict]:
    param = {
        "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"], "client": "web",
        "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default", "pageIndex": 1,
                                       "pageSize": size, "preTag": "", "postTag": ""}},
    }
    r = await client.get(
        "https://search-api-web.eastmoney.com/search/jsonp",
        params={"cb": "cb", "param": json.dumps(param, ensure_ascii=False)},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://so.eastmoney.com/"},
    )
    t = r.text
    d = json.loads(t[t.index("(") + 1: t.rindex(")")])
    return (d.get("result") or {}).get("cmsArticleWebOld") or []


async def _eastmoney(stock: dict, cutoff: str, client: httpx.AsyncClient) -> list[dict]:
    names = aliases(stock)
    out = []
    for kw in names:
        try:
            items = await _em_search(client, kw)
        except Exception:
            continue
        for x in items:
            title = _clean(x.get("title"))
            date = str(x.get("date") or "")[:10]
            if date < cutoff or not any(a.lower() in title.lower() for a in names):
                continue  # 相关性：标题必须点名该公司
            out.append({"title": title, "source": x.get("mediaName") or "东方财富", "time": date,
                        "ts": str(x.get("date") or ""), "url": x.get("url"),
                        "summary": _clean(x.get("content"))[:300]})
    return out


async def _finnhub(stock: dict, cutoff: str, client: httpx.AsyncClient) -> list[dict]:
    key = env_key("FINNHUB_API_KEY")
    code = stock["code"]
    if not key or not code.endswith(".US"):
        return []
    sym = code.split(".")[0]
    try:
        r = await client.get("https://finnhub.io/api/v1/company-news",
                             params={"symbol": sym, "from": cutoff, "to": dt.date.today().isoformat(), "token": key})
        rows = r.json() if r.status_code == 200 else []
    except Exception:
        return []
    names = EN_ALIAS.get(code, [sym])
    out = []
    for x in rows or []:
        head = _clean(x.get("headline"))
        # Finnhub 的 company-news 混有大量泛市场稿（查 NVDA 会返回特斯拉理财文），必须点名过滤
        if not any(n.lower() in head.lower() for n in names):
            continue
        ts = dt.datetime.fromtimestamp(x.get("datetime") or 0)
        out.append({"title": head, "source": f"{x.get('source') or 'Finnhub'}（英文）", "time": ts.date().isoformat(),
                    "ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "url": x.get("url"), "lang": "en",
                    "summary": _clean(x.get("summary"))[:300]})
    return out[:15]


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _similar(a: str, b: str) -> bool:
    x, y = _bigrams(a), _bigrams(b)
    return bool(x and y) and len(x & y) / len(x | y) >= 0.55


def diversify(items: list[dict], cap: int = PER_STOCK_CAP) -> list[dict]:
    items = sorted(items, key=lambda x: x.get("ts") or x["time"], reverse=True)
    # 1) 数字模板合并：同一模板只留最新一条
    by_tpl: dict[str, dict] = {}
    for it in items:
        tpl = _NUM_RE.sub("#", it["title"])
        if tpl in by_tpl:
            by_tpl[tpl]["_merged"] = by_tpl[tpl].get("_merged", 1) + 1
        else:
            by_tpl[tpl] = it
    uniq = list(by_tpl.values())
    # 2) 跨媒体去重
    kept: list[dict] = []
    for it in uniq:
        dup = next((k for k in kept if _similar(k["title"], it["title"])), None)
        if dup:
            dup["_merged"] = dup.get("_merged", 1) + it.get("_merged", 1)
        else:
            kept.append(it)
    # 3) 例行快讯族限额（同族同日只留 1 条，全期最多 ROUTINE_CAP 条）+ 单日限额（防热点当天刷屏）
    fam_count: dict[str, int] = {}
    fam_day: set[tuple[str, str]] = set()
    day_count: dict[str, int] = {}
    out = []
    for it in kept:
        fam = next((f for f, p in ROUTINE_PATTERNS.items() if p.search(it["title"])), None)
        if fam:
            if (fam, it["time"]) in fam_day or fam_count.get(fam, 0) >= ROUTINE_CAP:
                continue
            fam_day.add((fam, it["time"]))
            fam_count[fam] = fam_count.get(fam, 0) + 1
        if day_count.get(it["time"], 0) >= PER_DAY_CAP:
            continue
        day_count[it["time"]] = day_count.get(it["time"], 0) + 1
        if it.get("_merged", 1) > 1:
            it["summary"] = (it.get("summary") or "") + f"（同类/同事件报道 {it['_merged']} 条已合并）"
        it.pop("_merged", None)
        out.append(it)
        if len(out) >= cap:
            break
    return out


async def fetch_news(stock: dict, days: int = 21) -> list[dict]:
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    async with httpx.AsyncClient(timeout=15) as client:
        em, fh = await asyncio.gather(_eastmoney(stock, cutoff, client), _finnhub(stock, cutoff, client))
    items = diversify(em + fh)
    for it in items:
        it.update({"code": stock["code"], "name": stock["name"]})
        it.pop("ts", None)
    return items
