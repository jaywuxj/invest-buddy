"""公告目录与监管文件采集（零 LLM，按实际链接区分来源等级）。

- 港股：东方财富公告库（np-anotice-stock，ann_type=H）转载 PDF；非港交所一手链接，需核验原文
- 美股：SEC EDGAR submissions API（免费，只需声明 User-Agent；10 req/s 限制，本模块逐股串行足够）

产出统一结构：{code, name, date, title, type, category, url, source}
- type：业绩公告 / 定期报告 / 回购 / 股权变动 / 管理层 / 资本运作 / 监管文件 / 其他
- category：映射到面板事件分类（业绩/回购/股权变动/管理层/资本运作/监管/经营），None 表示不进事件流

降噪规则：
- 港股「翌日披露报表」（每日回购申报）→ 合并为 1 条「近 N 日回购申报 X 份」
- 港股「通知信函及回条」「月报表」等纯行政文件 → 不进事件流（仍可在公告列表看到月报表最新 1 份）
- 美股 Form 4 / 144（内部人交易/拟售）→ 合并为 1 条汇总
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

SEC_UA = "invest-harness research contact@learnwithai.com.cn"
_CIK_CACHE: dict[str, int] = {}

HK_COLUMN_MAP = [  # (关键词, type, category)；先匹配栏目、再匹配标题，行政类排最前
    (r"月報表|月报表|暫停辦理|暂停办理|過戶|过户|股東大會通告|股东大会通告|股東特別大會|股东特别大会|股东会通告|翌日披露", "其他", None),
    (r"業務發展|业务发展|產銷|产销|營運|营运|交付|快報|快报", "经营公告", "经营"),
    (r"業績|业绩", "业绩公告", "业绩"),
    (r"(中期|年度|半年度|季度|第[一二三四]季度)(報告|报告)|年報|年报|中報|中报", "定期报告", "业绩"),
    (r"購回|购回", "回购", "回购"),
    (r"董事|委任|辭任|辞任|公司秘書|公司秘书|授權代表", "管理层", "管理层"),
    (r"股份計劃|股份计划|購股權|购股权|股權披露|权益", "股权变动", "股权变动"),
    (r"收購|收购|出售|配售|關連|关连|須予披露|须予披露|發行|发行|供股|分拆|債券|债券", "资本运作", "资本运作"),
    (r"股息|分派|派息", "资本运作", "资本运作"),
]
HK_EMPTY_TITLE = re.compile(r"^(海外监管公告|海外監管公告|公告|自愿公告|自願公告)$")
HK_NOISE_TITLE = re.compile(r"通知信函|回条|回條|委任表格|代表委任|股东大会通告之补充|更正")

US_8K_ITEMS = {
    "1.01": "重大协议", "1.02": "终止重大协议", "2.01": "完成收购/处置", "2.02": "业绩发布",
    "2.03": "新增财务义务", "2.05": "重组/退出成本", "2.06": "资产减值", "3.02": "未注册股权发行",
    "5.02": "董事/高管变动", "5.03": "章程修订", "5.07": "股东大会投票结果", "7.01": "FD 披露",
    "8.01": "其他重大事项", "9.01": "财务报表与附件",
}
US_6K_HINT = [
    (r"revenue", "月度营收报告", "业绩公告", "业绩"),
    (r"(fs|financial|quarter|earn)", "季度财报", "定期报告", "业绩"),
    (r"dividend", "股利相关", "资本运作", "资本运作"),
    (r"board", "董事会决议", "管理层", "管理层"),
    (r"month", "月末公告", "其他", None),
]


def primary_link(url: str, code: str) -> bool:
    """仅凭可信交易所域名判断链接是否直达一手披露，不凭标题/源字段猜测。"""
    try:
        parsed = urlsplit(str(url))
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        return False
    if code.upper().endswith(".HK"):
        return host in ("hkexnews.hk", "hkex.com.hk") or host.endswith((".hkexnews.hk", ".hkex.com.hk"))
    if code.upper().endswith(".US"):
        return host == "sec.gov" or host.endswith(".sec.gov")
    return False


def normalize_panel_sources(name: str, data: Any) -> Any:
    """兼容旧自产缓存：读取时修正错误的一手标注，不修改磁盘或用户资料。"""
    if not isinstance(data, dict) or name not in ("events", "earnings"):
        return data
    if name == "events":
        rows = data.get("items")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                url, code = str(row.get("url") or ""), str(row.get("code") or "")
                if primary_link(url, code):
                    continue
                if (row.get("official") or row.get("source_type") == "primary" or
                        ("pdf.dfcfw.com" in url and "官方公告原文" in str(row.get("desc") or ""))):
                    row["official"] = False
                    row["source_type"] = "secondary"
                    row["desc"] = "第三方公告库转载链接，非交易所一手链接；请核对交易所披露原文"
                    row["type"] = re.sub(r"^港交所披露易\s*·\s*", "东方财富公告库转载 · ", str(row.get("type") or ""))
        if "官方公告（港交所披露易 / SEC EDGAR" in str(data.get("source") or ""):
            data["source"] = "港股：东方财富公告库转载（非一手，需核验原文）；美股：SEC EDGAR 官方链接；媒体：东方财富 / Finnhub。公告按链接域名逐条核验"
    else:
        items = data.get("items")
        if isinstance(items, dict):
            for code, stock in items.items():
                if not isinstance(stock, dict) or not isinstance(stock.get("announcements"), list):
                    continue
                for row in stock["announcements"]:
                    if isinstance(row, dict):
                        verified = primary_link(str(row.get("url") or ""), code)
                        row["source_type"] = "primary" if verified else "secondary"
                        row["source"] = ("港交所披露易" if code.endswith(".HK") else "SEC EDGAR") if verified else "第三方公告库（需核验原文）"
        if isinstance(data.get("sources"), list):
            data["sources"] = ["相关公告：港股东方财富公告库转载（非一手，需核验原文）/ 美股 SEC EDGAR"
                               if "相关公告：港交所披露易（经东方财富公告库）" in str(s) else s
                               for s in data["sources"]]
    return data


# ---------------------------------------------------------------- 港股

async def _hk(stock: dict, since: str, client: httpx.AsyncClient) -> list[dict]:
    sym = stock["code"].split(".")[0]
    r = await client.get(
        "https://np-anotice-stock.eastmoney.com/api/security/ann",
        params={"sr": -1, "page_size": 50, "page_index": 1, "ann_type": "H", "client_source": "web", "stock_list": sym},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    rows = ((r.json() or {}).get("data") or {}).get("list") or []
    out, buyback, empty = [], [], []
    for x in rows:
        date = str(x.get("notice_date") or "")[:10]
        if date < since:
            continue
        title = str(x.get("title_ch") or x.get("title") or "").strip()
        col = ((x.get("columns") or [{}])[0] or {}).get("column_name") or ""
        art = x.get("art_code")
        item = {"date": date, "title": title, "url": f"https://pdf.dfcfw.com/pdf/H2_{art}_1.pdf", "source": "东方财富公告库转载（原文待核验）"}
        if "翌日披露" in title:
            buyback.append(item)
            continue
        if HK_NOISE_TITLE.search(title):
            continue
        if HK_EMPTY_TITLE.match(title):
            empty.append(item)  # 标题无信息量（多为 A 股同步披露的海外监管公告）：合并
            continue
        typ, cat = "其他", None  # 无法归类的默认不进事件流，只在公告列表可见
        hit = next(((t, c) for pat, t, c in HK_COLUMN_MAP if re.search(pat, col)), None) \
            or next(((t, c) for pat, t, c in HK_COLUMN_MAP if re.search(pat, title)), None)
        if hit:
            typ, cat = hit
        out.append({**item, "type": typ, "category": cat, "column": col})
    if empty:
        out.append({"date": empty[0]["date"], "url": empty[0]["url"], "source": "东方财富公告库转载（原文待核验）",
                    "title": f"海外监管公告（A 股同步披露）{len(empty)} 份，最近 {empty[0]['date'][5:]}",
                    "type": "其他", "category": None, "column": "海外監管公告"})
    if buyback:
        buyback.sort(key=lambda z: z["date"], reverse=True)
        out.append({"date": buyback[0]["date"], "url": buyback[0]["url"], "source": "东方财富公告库转载（原文待核验）",
                    "title": f"股份回购：{buyback[-1]['date'][5:]} 至 {buyback[0]['date'][5:]} 共提交翌日披露报表 {len(buyback)} 份",
                    "type": "回购", "category": "回购", "column": "股份購回"})
    return out


# ---------------------------------------------------------------- 美股

async def _cik(client: httpx.AsyncClient, ticker: str) -> int | None:
    if not _CIK_CACHE:
        r = await client.get("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": SEC_UA})
        for v in (r.json() or {}).values():
            _CIK_CACHE[str(v.get("ticker"))] = int(v.get("cik_str"))
    return _CIK_CACHE.get(ticker)


async def _us(stock: dict, since: str, client: httpx.AsyncClient) -> list[dict]:
    ticker = stock["code"].split(".")[0]
    cik = await _cik(client, ticker)
    if not cik:
        return []
    r = await client.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers={"User-Agent": SEC_UA})
    rec = ((r.json() or {}).get("filings") or {}).get("recent") or {}
    forms = rec.get("form") or []
    out, insider = [], {"4": 0, "144": 0, "first": None, "last": None, "url": None}
    for i, form in enumerate(forms):
        date = rec["filingDate"][i]
        if date < since:
            break  # recent 按日期倒序
        acc = rec["accessionNumber"][i].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{rec['primaryDocument'][i]}"
        if form in ("4", "144", "144/A", "4/A"):
            key = "144" if form.startswith("144") else "4"
            insider[key] += 1
            insider["last"] = insider["last"] or date
            insider["first"] = date
            insider["url"] = insider["url"] or f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4"
            continue
        if form == "8-K":
            items = [s.strip() for s in str(rec["items"][i] or "").split(",") if s.strip()]
            names = [US_8K_ITEMS.get(s, s) for s in items if s != "9.01"] or ["重大事项"]
            cat = "业绩" if "2.02" in items else "管理层" if "5.02" in items else "资本运作" if any(s in items for s in ("1.01", "2.01", "3.02")) else "经营"
            typ = "业绩公告" if "2.02" in items else "监管文件"
            out.append({"date": date, "title": f"8-K：{'、'.join(names)}", "type": typ, "category": cat, "url": url, "source": "SEC EDGAR"})
        elif form in ("10-Q", "10-K", "20-F", "40-F"):
            label = {"10-Q": "季度报告 10-Q", "10-K": "年度报告 10-K", "20-F": "年度报告 20-F", "40-F": "年度报告 40-F"}[form]
            out.append({"date": date, "title": label, "type": "定期报告", "category": "业绩", "url": url, "source": "SEC EDGAR"})
        elif form == "6-K":
            doc = str(rec["primaryDocument"][i]).lower()
            title, typ, cat = "6-K 外国发行人报告", "监管文件", None
            for pat, t, ty, c in US_6K_HINT:
                if re.search(pat, doc):
                    title, typ, cat = f"6-K：{t}", ty, c
                    break
            out.append({"date": date, "title": title, "type": typ, "category": cat, "url": url, "source": "SEC EDGAR"})
        elif form.startswith("SC 13") or form.startswith("SCHEDULE 13"):
            out.append({"date": date, "title": f"{form}：5% 以上股东持股申报", "type": "股权变动", "category": "股权变动", "url": url, "source": "SEC EDGAR"})
        elif form in ("DEF 14A", "S-8", "S-3", "424B2", "424B5"):
            out.append({"date": date, "title": f"{form}", "type": "资本运作" if form != "DEF 14A" else "监管文件",
                        "category": None, "url": url, "source": "SEC EDGAR"})
    if insider["4"] or insider["144"]:
        parts = []
        if insider["4"]:
            parts.append(f"Form 4 内部人交易申报 {insider['4']} 份")
        if insider["144"]:
            parts.append(f"Form 144 拟出售申报 {insider['144']} 份")
        out.append({"date": insider["last"], "title": f"内部人交易：{insider['first'][5:]} 至 {insider['last'][5:]} " + "，".join(parts),
                    "type": "股权变动", "category": "股权变动", "url": insider["url"], "source": "SEC EDGAR"})
    return out


# ---------------------------------------------------------------- 入口

async def fetch_filings(stocks: list[dict], days: int = 60) -> dict[str, list[dict]]:
    """返回 code → 公告列表（新→旧）。单股失败返回空列表，不影响其它。"""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    result: dict[str, list[dict]] = {}
    async with httpx.AsyncClient(timeout=15) as client:
        async def one(s: dict):
            try:
                fn = _hk if s["code"].endswith(".HK") else _us
                items = await fn(s, since, client)
            except Exception:
                items = []
            for it in items:
                it.update({"code": s["code"], "name": s["name"]})
            items.sort(key=lambda z: z["date"], reverse=True)
            result[s["code"]] = items

        hk = [s for s in stocks if s["code"].endswith(".HK")]
        us = [s for s in stocks if not s["code"].endswith(".HK")]
        await asyncio.gather(*(one(s) for s in hk))
        for s in us:  # SEC 限速：串行
            await one(s)
    return result


def earnings_announcements(items: list[dict], limit: int = 6) -> list[dict]:
    """财报 Tab 的「财报原文」：只留业绩公告与定期报告（2026-10-15：不再用回购/董事会等其他公告补位，那些在「动态」）。"""
    pri = [x for x in items if x["type"] in ("业绩公告", "定期报告")]
    rest: list[dict] = []
    return [{"title": x["title"], "date": x["date"], "url": x["url"], "type": x["type"],
             "source_type": "primary" if primary_link(x["url"], x.get("code", "")) else "secondary",
             "source": x.get("source")} for x in (pri + rest)[:limit]]
