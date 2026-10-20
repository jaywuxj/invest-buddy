"""联网搜索工具层（移植 8899 websearch.js）。

  web_search(query)   通用网页搜索：付费 provider（有 Key 才启用）优先 → 免费 HTML 源（搜狗/360/百度）逐个兜底
  stock_news(code)    个股最新资讯/公告：本机 westock CLI（腾讯自选股源，免费、结构化）
  read_url(url)       读取网页正文：westock 资讯链接走 `westock news detail`，其余抓取→去标签→截断

隐私边界（本项目的硬约束）：
  - 搜索 query 只允许公开实体（公司简称/行业/宏观词），严禁携带持仓数量、成本、仓位、盈亏等私有数据；
    服务端在发出搜索前做敏感值扫描（guard_query），命中即拒绝并要求改写。
  - read_url 只放行 http(s) 公网地址（拒绝 localhost/内网/链路本地，防 SSRF）；
    返回内容只作「资料」，由 ask 的系统提示隔离指令。
成本：免费源 0 元；博查 ≈ ¥0.036/次，Tavily 免费 1000 次/月。Key 写 .env（BOCHA_API_KEY / TAVILY_API_KEY）。
缓存：同一 query/url 10 分钟内命中内存缓存。
"""
from __future__ import annotations

import asyncio
import html as _html
import ipaddress
import re
import socket
import time
from typing import Any
from urllib.parse import quote as urlquote
from urllib.parse import urlparse

import httpx

from . import portfolio, settings
from .app_settings import get_settings, runtime_secret
from .providers import westock

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")
TTL_SEC = 600
MAX_HTML = 2 * 1024 * 1024

PROVIDERS = [
    ("bocha", "博查", "paid", "BOCHA_API_KEY"),
    ("tavily", "Tavily", "paid", "TAVILY_API_KEY"),
    ("sogou", "搜狗", "free", None),
    ("so360", "360 搜索", "free", None),
    ("baidu", "百度", "free", None),
]

_cache: dict[str, tuple[float, dict]] = {}


def _cached(key: str):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL_SEC:
        return {**hit[1], "cached": True}
    return None


def _store(key: str, v: dict) -> dict:
    _cache[key] = (time.time(), v)
    if len(_cache) > 200:
        _cache.pop(next(iter(_cache)))
    return v


def _strip(h: str) -> str:
    h = re.sub(r"<!--[\s\S]*?-->", "", str(h or ""))
    h = re.sub(r"<(script|style|noscript)[\s\S]*?</\1>", "", h, flags=re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    h = re.sub(r"<[^>]*>?", " ", h)
    return re.sub(r"\s+", " ", _html.unescape(h)).strip()


def _cut(s: str, n: int) -> str:
    s = str(s or "")
    return s[:n] + "…" if len(s) > n else s


def _host(u: str) -> str:
    try:
        return urlparse(u).hostname.replace("www.", "") if u else ""
    except Exception:
        return ""


def _pick_date(s: str) -> str:
    s = str(s or "")
    m = re.search(r"(20\d\d)[-年/.](\d{1,2})[-月/.](\d{1,2})", s)
    if m:
        return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}"
    m = re.search(r"(\d+)\s*(分钟|小时|天)前", s)
    if m:
        ms = {"分钟": 60, "小时": 3600, "天": 86400}[m[2]] * int(m[1])
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600 - ms))
    return ""


async def _get_text(url: str, *, json_mode: bool = False, timeout: float = 10,
                    headers: dict | None = None, body: bytes | None = None,
                    method: str = "GET") -> dict:
    hdrs = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Accept": "application/json" if json_mode else "text/html,application/xhtml+xml,*/*;q=0.8"}
    hdrs.update(headers or {})
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        r = await (client.post(url, content=body, headers=hdrs) if method == "POST"
                   else client.get(url, headers=hdrs))
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    raw = r.content[:MAX_HTML]
    ct = r.headers.get("content-type") or ""
    cs = None
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    if m:
        cs = m[1]
    if not cs and not json_mode:
        m = re.search(r'<meta[^>]+charset=["\']?([\w-]+)', raw[:4096].decode("latin1"), re.I)
        if m:
            cs = m[1]
    enc = (cs or "utf-8").lower().replace("gb2312", "gbk")
    try:
        txt = raw.decode(enc, errors="replace")
    except LookupError:
        txt = raw.decode("utf-8", errors="replace")
    return {"text": txt, "url": str(r.url), "ct": ct}


# ---------- 免费 HTML 源 ----------

async def _sogou(q: str, n: int) -> list[dict]:
    h = (await _get_text("https://www.sogou.com/web?query=" + urlquote(q)))["text"]
    if re.search(r"antispider|请输入验证码", h[:20000]):
        raise RuntimeError("搜狗触发验证")
    out = []
    for b in re.split(r'<div class="vrwrap"|<div class="rb"', h)[1:]:
        hm = re.search(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', b)
        if not hm:
            continue
        url = _html.unescape(hm[1])
        if url.startswith("/"):
            url = "https://www.sogou.com" + url
        title = _strip(hm[2])
        if not title:
            continue
        cite = _strip((re.search(r'class="[^"]*citeurl[^"]*"[^>]*>([\s\S]*?)</(?:cite|div|span)>', b)
                       or re.search(r'citeLinkClass[^>]*>([\s\S]{0,300}?)</(?:div|span|p)>', b)
                       or type("M", (), {"group": lambda s, i: ""})()).group(1) or "")
        body = b[b.find("</h3>") + 5:].split("citeLinkClass")[0].split('class="citeurl')[0]
        snip = re.sub(r"^(推荐您搜索|大家还在搜)\s*", "", _strip(body))
        site_m = re.match(r"^[^\s\dhttp][^\s]{0,12}", cite)
        out.append({"title": title, "url": url,
                    "site": (site_m[0] if site_m else "") or _host(url),
                    "date": _pick_date(cite) or _pick_date(snip[:40]) or _pick_date(_strip(b)[-200:]),
                    "snippet": _cut(snip, 240)})
        if len(out) >= n:
            break
    return out


async def _so360(q: str, n: int) -> list[dict]:
    h = (await _get_text("https://www.so.com/s?q=" + urlquote(q)))["text"]
    out = []
    for b in h.split('<li class="res-list"')[1:]:
        hm = re.search(r'<h3[^>]*>\s*<a([^>]*)>([\s\S]*?)</a>', b)
        if not hm:
            continue
        attrs = hm[1]
        url = _html.unescape((re.search(r'data-mdurl="([^"]+)"', attrs) or [None, ""])[1]
                             or (re.search(r'href="([^"]+)"', attrs) or [None, ""])[1])
        if re.search(r"ai\.so\.com|so\.com/s\?", url):
            continue
        title = _strip(hm[2])
        if not title or re.search(r"相关新闻$|其他人还搜了", title):
            continue
        snip = _strip((re.search(r'class="(?:res-desc|res-list-summary)[^"]*"[^>]*>([\s\S]*?)</(?:p|span|div)>', b)
                       or [None, b[b.find("</h3>") + 5:].split("g-linkinfo")[0]])[1])
        gray = _strip((re.search(r'class="g-c-gray"[^>]*>([\s\S]*?)</span>', b) or [None, ""])[1])
        out.append({"title": title, "url": url, "site": _host(url),
                    "date": _pick_date(gray) or _pick_date(snip[:30]), "snippet": _cut(snip, 240)})
        if len(out) >= n:
            break
    return out


async def _baidu(q: str, n: int) -> list[dict]:
    h = (await _get_text("https://www.baidu.com/s?ie=utf-8&wd=" + urlquote(q)))["text"]
    if "<title>百度安全验证</title>" in h:
        raise RuntimeError("百度触发安全验证")
    out = []
    for b in re.split(r'<div[^>]+class="result c-container', h)[1:]:
        mu = (re.search(r'\bmu="([^"]+)"', b) or [None, None])[1]
        hm = re.search(r"<h3[^>]*>([\s\S]*?)</h3>", b)
        if not hm:
            continue
        title = _strip(hm[1])
        if not title:
            continue
        href = (re.search(r'href="([^"]+)"', hm[1]) or [None, ""])[1]
        url = _html.unescape(mu or href or "")
        body = re.sub(r"<style[\s\S]*?</style>", "", b[b.find("</h3>") + 5:])
        snip = re.split(r'"\}\],|"clamp"|\{"', _strip(body))[0]
        snip = re.sub(r"^(视频|文章)\s*", "", snip)
        out.append({"title": title, "url": url, "site": _host(url),
                    "date": _pick_date(snip[:40]), "snippet": _cut(snip, 240)})
        if len(out) >= n:
            break
    return out


_FREE = {"sogou": _sogou, "so360": _so360, "baidu": _baidu}


# ---------- 付费 API（有 Key 才启用） ----------

async def _bocha(q: str, n: int, key: str) -> list[dict]:
    import json
    r = await _get_text("https://api.bochaai.com/v1/web-search", json_mode=True, timeout=12,
                        method="POST",
                        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                        body=json.dumps({"query": q, "freshness": "noLimit", "summary": True, "count": n}).encode())
    j = json.loads(r["text"])
    vals = ((j.get("data") or j).get("webPages") or {}).get("value") or []
    return [{"title": x.get("name"), "url": x.get("url"),
             "site": x.get("siteName") or _host(x.get("url") or ""),
             "date": str(x.get("datePublished") or x.get("dateLastCrawled") or "")[:10],
             "snippet": _cut(x.get("summary") or x.get("snippet"), 400)} for x in vals[:n]]


async def _tavily(q: str, n: int, key: str) -> list[dict]:
    import json
    r = await _get_text("https://api.tavily.com/search", json_mode=True, timeout=15,
                        method="POST",
                        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                        body=json.dumps({"query": q, "max_results": n, "search_depth": "basic"}).encode())
    j = json.loads(r["text"])
    return [{"title": x.get("title"), "url": x.get("url"), "site": _host(x.get("url") or ""),
             "date": str(x.get("published_date") or "")[:10],
             "snippet": _cut(x.get("content"), 400)} for x in (j.get("results") or [])[:n]]


_PAID = {"bocha": _bocha, "tavily": _tavily}


def providers() -> list[dict]:
    on = (get_settings().get("search") or {}).get("providers") or {}
    paid = [{"id": p[0], "name": p[1], "paid": True, "key": runtime_secret(p[3])}
            for p in PROVIDERS if p[2] == "paid" and on.get(p[0]) is not False and runtime_secret(p[3])]
    free = [{"id": p[0], "name": p[1], "paid": False}
            for p in PROVIDERS if p[2] == "free" and on.get(p[0]) is not False]
    return paid + free


def status() -> list[dict]:
    on = (get_settings().get("search") or {}).get("providers") or {}
    return [{"id": p[0], "name": p[1], "tier": p[2], "env": p[3],
             "enabled": on.get(p[0]) is not False,
             "has_key": bool(runtime_secret(p[3])) if p[3] else None} for p in PROVIDERS]


def search_available() -> bool:
    return bool(providers())


# ---------- 隐私护栏：搜索词不得携带持仓私有数据 ----------

def _private_tokens() -> list[str]:
    """持仓私有数值指纹：成本价、股数（只收集数值，不含代码/名称）。"""
    toks = []
    for s in portfolio.positions():
        for k in ("cost", "shares"):
            v = s.get(k)
            if isinstance(v, (int, float)) and v > 0:
                t = f"{v:g}"
                if len(t) >= 3:  # 过短的数字没有区分度
                    toks.append(t)
    return toks


def guard_query(query: str) -> str | None:
    """返回 None=放行；否则返回拒绝原因。"""
    q = str(query or "").strip()
    if not q:
        return "empty query"
    for tok in _private_tokens():
        if tok in q:
            return "搜索词包含持仓私有数值（成本/股数），已拦截；请仅用公开实体名称（公司/行业/宏观词）"
    if re.search(r"我的(持仓|成本|仓位|盈亏)|持仓(数量|成本|占比)|成本价|浮亏|浮盈", q):
        return "搜索词引用持仓私有信息，已拦截；请仅用公开实体名称（公司/行业/宏观词）"
    if re.search(r"我的?(现金|可用资金|总资产|净投入|已实现)|现金(余额|还剩|还有多少)|账户(余额|总资产)", q):
        return "搜索词引用账户私有信息（现金/总资产），已拦截；请仅用公开实体名称（公司/行业/宏观词）"
    try:
        from . import cash as cash_mod
        for cur, amount in cash_mod.balances()[0].items():
            if amount > 0 and f"{amount:g}" in q:
                return "搜索词包含现金私有数值，已拦截；请仅用公开实体名称（公司/行业/宏观词）"
    except Exception:
        pass
    return None


# ---------- 工具实现 ----------

async def web_search(query: str, n: int | None = None) -> dict:
    query = str(query or "").strip()[:120]
    g = guard_query(query)
    if g:
        return {"ok": False, "error": "privacy_guard", "detail": g, "results": []}
    n = max(3, min(10, n or int((get_settings().get("search") or {}).get("results") or 6)))
    hit = _cached(f"s:{query}:{n}")
    if hit:
        return hit
    tried, results, seen = [], [], set()
    for p in providers():
        t0 = time.time()
        try:
            rows = await (_PAID[p["id"]](query, n, p["key"]) if p["paid"] else _FREE[p["id"]](query, n))
            tried.append({"id": p["id"], "n": len(rows), "ms": int((time.time() - t0) * 1000)})
            for x in rows:
                k = re.sub(r"[?#].*$", "", x.get("url") or x.get("title") or "")
                if x.get("title") and k not in seen:
                    seen.add(k)
                    results.append({**x, "via": p["id"]})
            if len(results) >= min(n, 4):
                break
        except Exception as e:
            tried.append({"id": p["id"], "error": str(e)[:80], "ms": int((time.time() - t0) * 1000)})
    return _store(f"s:{query}:{n}", {"ok": bool(results), "query": query,
                  "provider": (next((t for t in tried if t.get("n")), {}) or {}).get("id", ""),
                  "tried": tried, "results": results[:n]})


def _gtimg_of(code: str) -> str | None:
    st = portfolio.stock_meta(code)
    return st.get("gtimg") if st else None


async def stock_news(code: str, kind: str = "all", n: int = 10) -> dict:
    """个股资讯/公告（westock CLI，联网数据源；code 只含公开标的代码）。"""
    ws = _gtimg_of(code)
    if not ws:
        return {"ok": False, "error": f"未知标的 {code}", "results": []}
    n = max(3, min(20, int(n or 10)))
    type_ = {"notice": "0", "report": "1", "news": "2", "all": "3"}.get(kind or "all", "3")
    hit = _cached(f"n:{ws}:{type_}:{n}")
    if hit:
        return hit
    try:
        txt = await asyncio.to_thread(westock.run, ["news", "list", ws, "--limit", str(n), "--type", type_], 15)
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "results": []}
    rows = (westock.tables(txt)[0]["rows"] if westock.tables(txt) else [])
    results = [{"id": r.get("id"), "title": r.get("title"), "url": r.get("url"),
                "site": r.get("src") or "", "date": str(r.get("time") or "")[:16],
                "snippet": _cut(r.get("summary") or "", 200), "kind": r.get("typeStr") or ""}
               for r in rows]
    return _store(f"n:{ws}:{type_}:{n}", {"ok": bool(results), "code": code,
                  "provider": "westock", "results": results})


def _is_private_host(hostname: str) -> bool:
    h = (hostname or "").lower().strip("[]")
    if re.fullmatch(r"localhost|.*\.local|.*\.internal", h):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
    except ValueError:
        return False


async def _assert_public(url: str) -> str:
    x = urlparse(url)
    if x.scheme not in ("http", "https"):
        raise ValueError("只支持 http(s)")
    h = x.hostname or ""
    if _is_private_host(h):
        raise ValueError("不允许访问本机或内网地址")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, h, None)
    except socket.gaierror:
        raise ValueError("域名解析失败")
    for info in infos:
        ip = info[4][0]
        if _is_private_host(ip):
            raise ValueError("不允许访问本机或内网地址")
    return url


def _main_text(h: str) -> dict:
    t = _strip((re.search(r"<title[^>]*>([\s\S]*?)</title>", h, re.I) or [None, ""])[1] or "")
    art = re.search(r"<article[\s\S]*?</article>", h, re.I)
    if art and len(art[0]) > 800:
        h = art[0]
    else:
        h = re.sub(r"<(nav|header|footer|aside|form)[\s\S]*?</\1>", " ", h, flags=re.I)
    h = re.sub(r"<!--[\s\S]*?-->", "", h)
    h = re.sub(r"<(script|style|noscript|svg)[\s\S]*?</\1>", "", h, flags=re.I)
    h = re.sub(r"<(br|/p|/div|/li|/h\d|/tr)[^>]*>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    body = "\n".join(l for l in (re.sub(r"\s+", " ", x).strip() for x in _html.unescape(h).split("\n"))
                     if len(l) >= 12 or (re.search(r"\d", l) and len(l) >= 6))
    return {"title": t, "text": body}


async def read_url(url: str, max_chars: int = 5000) -> dict:
    url = str(url or "").strip()
    max_chars = max(1000, min(12000, int(max_chars or 5000)))
    hit = _cached(f"u:{url}:{max_chars}")
    if hit:
        return hit
    try:
        sn = re.search(r"[?&#]id=nes(SN\w+)", url) or re.match(r"^(SN\w{10,})$", url)
        if sn:
            txt = await asyncio.to_thread(westock.run, ["news", "detail", sn[1]], 15)
            title = (re.search(r"^#\s*(.+)$", txt, re.M) or [None, ""])[1] or ""
            return _store(f"u:{url}:{max_chars}", {"ok": True, "url": url, "title": title,
                          "site": "westock", "text": _cut(_strip(re.sub(r"^#.*$", "", txt, flags=re.M)), max_chars)})
        x = await _assert_public(url)
        r = await _get_text(x, timeout=12)
        if r["url"] != x:
            await _assert_public(r["url"])
        # 搜狗/百度跳转页：取出真实地址再抓一次
        if len(r["text"]) < 4000:
            jump = (re.search(r'window\.location\.replace\("([^"]+)"\)', r["text"])
                    or re.search(r"URL='([^']+)'", r["text"])
                    or re.search(r'http-equiv="refresh"[^>]*url=([^"\'>]+)', r["text"], re.I))
            if jump:
                x2 = await _assert_public(_html.unescape(jump[1]))
                r = await _get_text(x2, timeout=12)
        m = _main_text(r["text"])
        if len(m["text"]) < 80:
            return {"ok": False, "url": url, "title": m["title"],
                    "error": "正文过短（可能需要登录或为动态页面）", "text": m["text"]}
        return _store(f"u:{url}:{max_chars}", {"ok": True, "url": r["url"] or url,
                      "title": m["title"], "site": _host(r["url"] or url),
                      "text": _cut(m["text"], max_chars)})
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)[:160]}


# ---------- DeepSeek/OpenAI 兼容 function calling 定义 ----------

def tool_defs(code: str) -> list[dict]:
    c = get_settings().get("search") or {}
    T = [{"type": "function", "function": {
        "name": "web_search",
        "description": "联网搜索网页（中文财经站点覆盖较好）。用于：面板资料没有的信息、今天/本周的最新动态、行业与竞品数据、政策与监管、需要交叉核实的数字。query 用简短关键词，只含公开实体（公司简称+时间词），例如「小米 SU7 9月交付量」。",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "搜索关键词，≤30 字，严禁包含持仓数量/成本/仓位/盈亏等私有数据"}},
                       "required": ["query"]}}}]
    if c.get("stock_news") is not False and code and code != "portfolio":
        T.append({"type": "function", "function": {
            "name": "stock_news",
            "description": "获取某只股票最新的资讯或公告列表（腾讯自选股源，结构化，带时间与来源，比网页搜索更新更准）。用户问「最新消息/今天发生了什么/有没有公告」时优先用它。",
            "parameters": {"type": "object", "properties": {
                "code": {"type": "string", "description": "股票代码，默认当前讨论的标的，如 01810.HK"},
                "kind": {"type": "string", "enum": ["all", "news", "notice", "report"],
                         "description": "all 全部 / news 新闻 / notice 公告 / report 研报"},
                "limit": {"type": "integer", "description": "条数 3-20，默认 10"}}}}})
    if c.get("read_url") is not False:
        T.append({"type": "function", "function": {
            "name": "read_url",
            "description": "读取某个网页或资讯的正文（来自 web_search / stock_news 返回的 url）。只在摘要不足以回答、需要原文细节时调用。",
            "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}})
    return T


async def run_tool(name: str, args: dict, ctx_code: str) -> dict:
    c = get_settings().get("search") or {}
    if name == "web_search":
        return await web_search(args.get("query"), c.get("results"))
    if name == "stock_news":
        return await stock_news(args.get("code") or ctx_code, args.get("kind"), args.get("limit"))
    if name == "read_url":
        return await read_url(args.get("url"))
    return {"ok": False, "error": f"unknown tool {name}"}
