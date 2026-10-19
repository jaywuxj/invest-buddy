"""面板契约全量构建（完全替代 WorkBuddy automation 的产出）。

流水线：
  ① 程序取数（panel_data.build_program_data）→ fundamentals / earnings / research 直接落盘
  ② panel-events   新闻 → 结构化事件      → events.json（+ 程序估算财报日历）
  ③ panel-analysis 全组合 → 信号/速览/情绪 → analysis.json
  ④ panel-themes   主题聚类               → themes.json（+ trends.json 空壳）
  ⑤ panel-onepager 单股一页纸（仅 full 模式，周更）→ onepager.json

每个 LLM 阶段走 run_skill_pipeline（Schema 校验 + 文本数字对账 + 失败保留 last-good），
阶段原始输出存 data/panel/_stage/ 供审计；任一阶段失败不影响其它阶段。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time
from typing import Any

from .. import filings, panel_data, portfolio, quality
from ..events import bus
from ..settings import PANEL_OUT_DIR
from ..skills import registry
from ..store import atomic_write_json, log_run, read_json
from .common import JobFailed, run_skill_pipeline

STAGE_DIR = PANEL_OUT_DIR / "_stage"
REDLINE_PCT = -50.0  # STRATEGY.md 止损红线（浮亏百分比）
RELEVANCE_MAX_DROP = 0.25  # 相关性过滤单批最大剔除比例，超过即熔断（模型曾误删 42%）
SENTIMENT_MIN_CONF = 0.85  # 情绪高置信阈值：低于此值一律中性（评测：0.8 档误判集中）
SENTIMENT_HARD_BASIS = {"E", "R", "C", "O", "L", "N", "D"}  # 硬信息类（见 skills/panel-sentiment/prompt.j2）
SENTIMENT_VOTES = 2        # 自洽性投票遍数：各遍方向一致才保留，否则中性
_INST_VIEW_RE = re.compile(r"目标价|评级|看高|看低|超配|低配|增持评级|买入评级|price target|upgrade|downgrade", re.I)

# 程序否决规则：模型常把以下「看似硬信息」判成方向，实际对价值预期无新增影响 → 强制中性
_PCT_PAIR_RE = re.compile(r"(\d+(?:\.\d+)?)%\s*(?:降至|升至|上升至|下降至|增至|减至)\s*(\d+(?:\.\d+)?)%")
_PCT_STEP_RE = re.compile(r"(?:上升|下降|增加|减少)\s*(\d+(?:\.\d+)?)%\s*至")
_SENTI_VETO = [
    (re.compile(r"连续\s*\d+\s*日回购|\d+月\d+日回购|斥资.{0,12}回购\d|回购\d+(?:\.\d+)?万股|翌日披露"), "例行回购执行（计划已公告）"),
    (re.compile(r"符合预期|基本符合|大致符合|in line with"), "业绩符合预期，无预期差"),
    (re.compile(r"余波|后[，,]?.{0,12}(再|又)(被|盯|查|罚)|回顾|复盘|一周年|两周年"), "旧事回顾/余波"),
]


def _holding_change_pp(text: str) -> float | None:
    """持股比例变动的百分点（如「从7.05%降至6.74%」→0.31；「上升0.05%至7.04%」→0.05）。"""
    m = _PCT_PAIR_RE.search(text)
    if m:
        return abs(float(m.group(1)) - float(m.group(2)))
    m = _PCT_STEP_RE.search(text)
    if m and re.search(r"持股|好仓|淡仓|权益", text):
        return float(m.group(1))
    return None


def _final_sentiment(v: Any, title: str = "", summary: str = "") -> tuple[str, str | None, float | None]:
    """情绪终判（程序规则）：默认中性；仅「硬信息 + 高置信 + 未被否决」保留利好/利空。
    旧版只看标题且无中性标准，人工标注集准确率仅 41%（产品发布/合作/股价上涨被大量判为利好）。"""
    if not isinstance(v, dict):
        return "中性", None, None  # 兼容旧格式 / 模型漏字段：宁可中性
    label = v.get("label") if v.get("label") in ("利好", "利空", "中性") else "中性"
    basis = str(v.get("basis") or "").strip().upper()[:1] or None
    try:
        conf = float(v.get("confidence"))
    except (TypeError, ValueError):
        conf = None
    if label != "中性" and (basis not in SENTIMENT_HARD_BASIS or conf is None or conf < SENTIMENT_MIN_CONF):
        label = "中性"
    if label != "中性":
        text = f"{title} {summary}"
        pp = _holding_change_pp(text) if re.search(r"持股|好仓|淡仓|权益|BlackRock|贝莱德", text) else None
        if pp is not None and pp < 1.0:
            label = "中性"  # 机构持股比例变动 < 1 个百分点：日常调仓噪音
        elif any(p.search(title) for p, _ in _SENTI_VETO):
            label = "中性"
        elif basis in ("E", "O") and not re.search(r"\d", title):
            label = "中性"  # 业绩/订单类必须在标题里有量化数字，否则多为观点或展望
        elif basis == "R" and re.search(r"维持", title) and not re.search(r"上调|下调|目标价|看高|看低|超配|低配|upgrade|downgrade", title, re.I):
            label = "中性"  # 维持原评级：无新增信息
    return label, basis, conf
SECTIONS = [  # (id, num, title, lead, embed, 字段类型)  2026-10-15：11 章精简为 6 章 + 程序数据速览（用户确认）
    ("summary", "01", "一句话看懂", None, None, "p"),
    ("business", "02", "怎么赚钱", None, None, "lead_bullets"),
    ("numbers", "03", "财务与估值速览", None, "numbers", None),   # 程序用实时数据渲染，模型不写
    ("industry", "04", "行业与竞争", None, None, "p"),
    ("bullbear", "05", "看多 vs 看空", None, None, "bullbear"),
    ("catalyst", "06", "接下来看什么", None, None, "table"),
    ("risks", "07", "主要风险", None, None, "bullets"),
]
# 一页纸不写会每天变化的数字（价格、估值倍数、目标价、市值、涨跌幅），也不写个人持仓：这些由页面实时数据展示
_OP_PRIVATE = re.compile(r"本组合|持仓|浮盈|浮亏|仓位|红线|成本\s*[0-9]|[0-9]+\s*股[，,；;。]|数量\s*[0-9]")


def _scrub_private(text: str) -> str:
    """兜底：删除含个人持仓信息的句子（模型不应写，但要保证即使写了也不上页面）。"""
    parts = re.split(r"(?<=[。；;！!？?])", str(text or ""))
    return "".join(x for x in parts if not _OP_PRIVATE.search(x)).strip()


def _onepager_hints(d: dict) -> dict:
    """给模型的估值/位置定性提示（纯文字，不含数字），让它能判断贵不贵而不写会过期的数。"""
    v, r = d.get("valuation") or {}, d.get("research") or {}
    pe = v.get("pe_forward") or v.get("pe_ttm")
    up = r.get("upside_pct")
    return {
        "pe_level": None if not pe else ("亏损或无意义" if pe <= 0 else "偏低" if pe < 15 else "适中" if pe <= 30 else "偏高"),
        "target_vs_price": None if up is None else ("机构目标价明显高于现价" if up >= 30 else "机构目标价高于现价" if up >= 10
                                                    else "机构目标价与现价接近" if up > -10 else "机构目标价低于现价"),
        "rating_mood": None if not r.get("rating_latest") else ("机构普遍看好" if (r["rating_latest"].get("buy") or 0) >= 70 else "机构看法分化"),
    }


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _write(name: str, obj: dict) -> None:
    atomic_write_json(PANEL_OUT_DIR / f"{name}.json", obj)


def _position_view(s: dict, q: dict | None) -> dict:
    price = q.get("price") if q else None
    v: dict[str, Any] = {"role": "持仓" if s.get("type") == "holding" or s.get("cost") else "关注"}
    if s.get("cost") and price:
        pnl = round((price - s["cost"]) / s["cost"] * 100, 2)
        v.update({"cost": s["cost"], "shares": s.get("shares"), "pnl_pct": pnl})
        # 红线判定由程序给出（模型曾把 -45% 误判为「已跌破 -50%」）
        if pnl <= REDLINE_PCT:
            v["redline"] = f"已触及 {REDLINE_PCT}% 止损红线"
        elif pnl <= REDLINE_PCT + 10:
            v["redline"] = f"接近红线：距 {REDLINE_PCT}% 还差 {round(pnl - REDLINE_PCT, 2)} 个百分点，尚未触及"
        else:
            v["redline"] = "未触及红线"
    return v


def _research_view(r: dict, price: float | None) -> dict:
    v = {k: r.get(k) for k in ("target_avg", "target_min", "target_max", "target_count", "rating_latest", "next_report")}
    avg = r.get("target_avg")
    if avg and price:
        # 两个口径都给出并命名清楚，模型只引用不计算
        v["upside_pct"] = round((avg - price) / price * 100, 2)            # 现价→目标均价的上行空间
        v["margin_of_safety_pct"] = round((avg - price) / avg * 100, 2)    # 相对目标均价的安全边际（框架线 ≥30%）
    return v


def _stock_digest(s: dict, pd: dict, *, compact: bool) -> dict:
    code = s["code"]
    q = pd["quotes"].get(code) or {}
    f = pd["fundamentals"]["items"].get(code) or {}
    e = pd["earnings"]["items"].get(code) or {}
    r = pd["research"]["items"].get(code) or {}
    d: dict[str, Any] = {
        "code": code, "name": s["name"], "sector": s.get("sector"),
        "price": q.get("price"), "pct_today": q.get("pct"), "currency": s.get("currency"),
        "position": _position_view(s, q),
        "valuation": {k: v for k, v in (f.get("valuation") or {}).items() if k != "note"},
        "market_cap_yi": f.get("market_cap"),
        "latest": f.get("latest"), "annual": f.get("annual"),
        "recent_quarters": (e.get("periods") or [])[: 2 if compact else 4],
        "research": _research_view(r, q.get("price")) if r else None,
        "next_earnings": next((u for u in pd["upcoming"] if u["code"] == code), None),
        # 公告目录标题（港股为转载，需核验原文），供信号/一页纸引用
        "filings": [{"date": f["date"], "type": f["type"], "title": f["title"]}
                    for f in (pd.get("filings") or {}).get(code, []) if f.get("category")][: 3 if compact else 8],
    }
    return d


# ---------------------------------------------------------------- ①b 新闻相关性 + 情绪（先于事件/信号）


_FRAGMENT_WORDS = ("当然", "然而", "而", "但", "不过", "随着", "此外", "另外", "与此同时", "同时", "对此",
                   "其中", "或许", "相信", "据悉", "目前看来", "值得注意的是", "需要指出的是",
                   "也就是说", "换句话说", "可以看出", "事实上", "实际上", "由此可见", "基于此",
                   "另一方面", "与之相比", "相比之下", "正如", "比如", "例如", "尽管", "虽然",
                   "即使", "由于", "因此", "于是", "所以", "并且")

def _looks_fragment(summary: str) -> bool:
    """摘要疑似断章（东财 content 是搜索关键词上下文片段而非导语）：
    ① 以承接连词/虚词开头；② 以半句标点或 lowercase 英文开头；③ 开头缺主语的残句。"""
    t = (summary or "").strip()
    if not t:
        return False
    if any(t.startswith(w) for w in _FRAGMENT_WORDS):
        return True
    if t[0] in "，。；：、）】”" or (t[0].isascii() and t[0].islower()):
        return True
    return False

# 相关性程序预剔除（先于 LLM，确定性规则）：
# ① 冒号前缀主语是其他主体（「一博科技：公司和英伟达…」主角是一博科技）；
# ② 对标句式（「蚂蚁灵光…对标 Meta Muse」主角是蚂蚁）。
_INST_PREFIX_OK = re.compile(
    r"^(花旗|高盛|摩根士丹利|摩根大通|大摩|小摩|中金|美银|美国银行|瑞银|汇丰|野村|麦格理|巴克莱|杰富瑞|派杰|"
    r"招银国际|中信证券|国泰君安|华泰|海通|招商|广发|国信|安信|兴业|晨星|惠誉|穆迪|标普|"
    r"早报|晚报|午报|收评|午评|盘前|盘后|快讯|要闻|研报|公告|观点|评论|独家|视频|直播|图说|数说|一图|专题)", re.I)
_SUBJECT_PREFIX = re.compile(r"^([\u4e00-\u9fa5A-Za-z0-9·&]{2,12})[：:]")
_RIVAL_RE = re.compile(r"(对标|媲美|叫板|挑战|平替|剑指)")


def _drop_by_subject(title: str, names: list[str]) -> str | None:
    """标题主语不是本公司 → 返回剔除原因；否则 None。"""
    t = title.strip()
    # ② 对标/挑战句式：本公司是被对标的对象，主角是竞品
    if _RIVAL_RE.search(t) and any(a.lower() in t.lower() for a in names) \
            and not any(t.lstrip("【[\"“（(").lower().startswith(a.lower()) for a in names):
        return "R：对标句式，主角是竞品（本公司仅被对标）"
    # ① 冒号前缀主语
    m = _SUBJECT_PREFIX.match(t)
    if not m:
        return None
    pre = m.group(1)
    if any(pre.lower() in a.lower() or a.lower() in pre.lower() for a in names):
        return None  # 前缀就是本公司（或别名互相包含，如「腾讯控股」前缀含「腾讯」）
    if _INST_PREFIX_OK.match(pre):
        return None  # 机构/栏目前缀，观点可能针对本公司
    return f"R：标题主语为「{pre}」，非本公司"


async def _stage_relevance(news: list[dict]) -> tuple[list[dict], dict]:
    """按 7 只股票一批并行判定：剔除「点名但主角是别人」的新闻，并给相关新闻打情绪。
    任一批失败 → 该批新闻全部保留、情绪置中性（宁可多留不误删）。"""
    skill = registry.get("panel-sentiment")
    codes = list(dict.fromkeys(n["code"] for n in news))
    groups = [set(codes[i:i + ANALYSIS_BATCH]) for i in range(0, len(codes), ANALYSIS_BATCH)]

    from .. import news as news_mod

    by_id = {n["id"]: n for n in news}
    stock_of = {s["code"]: s for s in portfolio.all_stocks()}

    def protected(n: dict) -> bool:
        """程序保护，模型不得剔除：①标题以公司名/别名开头（公司是主语）；
        ②综述稿里含针对该公司的机构目标价/评级/配置建议（如「花旗看高成分股腾讯至765港元」）。"""
        t = n["title"].lstrip("【[「“\"'（( ")
        names = news_mod.aliases(stock_of.get(n["code"], n))
        if any(t.lower().startswith(a.lower()) for a in names):
            return True
        return bool(_INST_VIEW_RE.search(t)) and any(a.lower() in t.lower() for a in names)

    async def one(i: int, g: set, rep: int):
        feed = [{"id": n["id"], "company": n["name"], "title": n["title"],
                 "summary": re.sub(r"（同类/同事件报道 \d+ 条已合并）", "", n.get("summary") or "")[:160]}
                for n in news if n["code"] in g]
        if rep:  # 第二遍打乱顺序，降低位置偏差对一致性检验的干扰
            feed = feed[::-1]
        return rep, feed, await run_skill_pipeline(skill, {"news_json": _dump(feed)},
                                                   STAGE_DIR / f"sentiment_b{i}_r{rep}.json")

    # 程序预剔除：主语非本公司的条目不进模型（确定性规则，省 token 且不误留）
    pre_drop = {}
    for n in news:
        reason = _drop_by_subject(n["title"], news_mod.aliases(stock_of.get(n["code"], n)))
        if reason and not protected(n):
            pre_drop[n["id"]] = reason
    if pre_drop:
        news = [n for n in news if n["id"] not in pre_drop]
        codes = list(dict.fromkeys(n["code"] for n in news))
        groups = [set(codes[i:i + ANALYSIS_BATCH]) for i in range(0, len(codes), ANALYSIS_BATCH)]

    # 自洽性：同一批跑 SENTIMENT_VOTES 遍，方向必须每遍一致才保留，否则中性
    outs = await asyncio.gather(*(one(i, g, r) for i, g in enumerate(groups) for r in range(SENTIMENT_VOTES)),
                                return_exceptions=True)
    drop: dict[str, str] = {}
    votes: dict[str, list] = {}
    failed, tripped, guarded = 0, 0, 0
    for o in outs:
        if isinstance(o, Exception):
            failed += 1
            continue
        rep, feed, res = o
        for k, v in (res.get("sentiment") or {}).items():
            votes.setdefault(k, []).append(v)
        if rep:
            continue  # 相关性只采用第一遍结果（剔除要保守，不做投票放大）
        cand = {}
        for x in res.get("irrelevant") or []:
            n = by_id.get(x.get("id"))
            if not n:
                continue
            if protected(n):
                guarded += 1
                continue
            cand[n["id"]] = str(x.get("reason") or "")[:60]
        # 熔断：单批剔除超过 25% 说明判断失常，整批剔除作废（宁可多留不误删）
        if feed and len(cand) > max(2, int(len(feed) * RELEVANCE_MAX_DROP)):
            tripped += 1
            continue
        drop.update(cand)
    drop = {**pre_drop, **drop}
    kept = [n for n in news if n["id"] not in drop]
    unstable = 0
    for n in kept:
        vs = [_final_sentiment(v, n.get("title", ""), n.get("summary", "")) for v in votes.get(n["id"], [])]
        labels = {v[0] for v in vs}
        if len(vs) >= SENTIMENT_VOTES and len(labels) == 1:
            label, basis, conf = vs[0][0], vs[0][1], min((v[2] for v in vs if v[2] is not None), default=None)
        else:
            if len(labels) > 1:
                unstable += 1
            label, basis, conf = "中性", None, None  # 票数不足或各遍不一致 → 不够置信
        n["sentiment"] = label
        n["sentiment_basis"] = basis
        n["sentiment_conf"] = conf
    dropped = [{"code": n["code"], "name": n["name"], "title": n["title"], "source": n["source"],
                "time": n["time"], "reason": drop[n["id"]]} for n in news if n["id"] in drop]
    atomic_write_json(STAGE_DIR / "relevance_dropped.json",
                      {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "total": len(news), "dropped": dropped})
    dist = {k: sum(1 for n in kept if n["sentiment"] == k) for k in ("利好", "利空", "中性")}
    return kept, {"dropped": len(dropped), "guarded": guarded, "tripped_batches": tripped,
                  "failed_batches": failed, "unstable_to_neutral": unstable, "sentiment": dist,
                  "samples": dropped[:8]}


# ---------------------------------------------------------------- ② 事件

def _filing_events(fil: dict[str, list[dict]]) -> list[dict]:
    """公告目录转事件（程序映射，零 LLM）；仅核验的直达链接标记一手。"""
    out = []
    for items in fil.values():
        for f in items:
            if not f.get("category"):
                continue
            verified = filings.primary_link(f.get("url", ""), f["code"])
            source = ("港交所披露易" if f["code"].endswith(".HK") else "SEC EDGAR") if verified else f["source"]
            out.append({"code": f["code"], "name": f["name"], "date": f["date"], "category": f["category"],
                        "type": f"{source} · {f['type']}", "title": f["title"],
                        "desc": "一手公告链接，可核对原文" if verified else "第三方公告库转载，非交易所一手链接；请核对交易所披露原文",
                        "url": f["url"], "official": verified, "source_type": "primary" if verified else "secondary"})
    return out


_BRIEFS_CACHE = "event_briefs.json"


_PERIODIC_TYPE = {"年报", "中报", "季报", "业绩公告", "10-K", "10-Q", "定期报告"}
_PERIODIC_TITLE = re.compile(r"年度报告|中期报告|半年度报告|季度报告|业绩公告|中期业绩|全年业绩|季度业绩|"
                             r"止三个月|止六个月|止九个月|止十二个月|止年度|10-K|10-Q")


async def _attach_event_briefs(pd: dict, items: list[dict]) -> None:
    """为公告类条目生成 1-2 句白话解读。两个来源同一缓存（按 url）：
    ① events 披露类（_filing_events 产物，desc 是核验提示）；
    ② earnings.announcements 非定期报告（动态页「公告」official 类，body 原本为空）。
    护栏：数字必须 ⊆ 标题、≤80 字、禁承接词开头。生成后重写 earnings.json。"""
    briefs = read_json(PANEL_OUT_DIR / _BRIEFS_CACHE, {}) or {}

    def key_of(it: dict) -> str:
        return it.get("url") or (str(it.get("code")) + "|" + str(it.get("date")) + "|" + str(it.get("title")))

    targets: list[tuple[dict, str]] = []  # (原对象, name)
    for it in items:
        if it.get("desc") and "转载" not in it["desc"] and "核对" not in it["desc"]:
            continue  # 媒体事件已有模型归类的 desc
        targets.append((it, it.get("name") or ""))
    for er in (pd.get("earnings", {}).get("items") or {}).values():
        for a in er.get("announcements") or []:
            if a.get("type") in _PERIODIC_TYPE or _PERIODIC_TITLE.search(a.get("title") or ""):
                continue
            targets.append((a, er.get("name") or ""))

    pending: list[tuple[dict, str]] = []
    for tgt, name in targets:
        hit = briefs.get(key_of(tgt))
        if hit:
            tgt["brief"] = hit
        else:
            pending.append((tgt, name))
    if not pending:
        return
    try:
        skill = registry.get("panel-event-brief")
    except KeyError:
        return
    for off in range(0, len(pending), 30):
        part = pending[off:off + 30]
        feed = [{"id": f"e{i}", "company": name, "date": tgt.get("date") or "",
                 "category": tgt.get("category") or tgt.get("type") or "", "title": tgt.get("title") or ""}
                for i, (tgt, name) in enumerate(part)]
        try:
            res = await run_skill_pipeline(skill, {"events_json": _dump(feed)},
                                           STAGE_DIR / "event_briefs" / f"b{off // 30}.json")
        except Exception:
            continue  # 单批失败不阻断：公告保留原标题
        for i, (tgt, _name) in enumerate(part):
            text = str((res.get("brief") or {}).get(f"e{i}") or "").strip()
            if not text or len(text) > 80 or _looks_fragment(text):
                continue
            title_nums = set(re.findall(r"\d+(?:\.\d+)?%?", tgt.get("title") or ""))
            if not set(re.findall(r"\d+(?:\.\d+)?%?", text)) <= title_nums:
                continue  # 引入标题外数字 → 防编造
            tgt["brief"] = text
            briefs[key_of(tgt)] = text
    atomic_write_json(PANEL_OUT_DIR / _BRIEFS_CACHE, briefs)
    if any(a.get("brief") for er in (pd.get("earnings", {}).get("items") or {}).values()
           for a in er.get("announcements") or []):
        _write("earnings", pd["earnings"])  # brief 附加到公告条目后重写


_I18N_CACHE = "news_i18n.json"


async def _stage_summary(news: list[dict]) -> int:
    """全部新闻生成中文摘要（100-300 字目标 200；英文条目另出 title_cn）。
    缓存按新闻链接持久；护栏：数字 ⊆ 原文、长度 60-320，不合格保留原文摘要。"""
    cache = read_json(PANEL_OUT_DIR / _I18N_CACHE, {}) or {}

    def _key(n):
        return n.get("url") or n.get("id") or ""

    applied = 0
    pending = []
    for n in news:
        hit = cache.get(_key(n))
        if hit and hit.get("s") and 60 <= len(hit["s"]) <= 320:  # 缓存应用同走长度护栏（旧缓存规格不一）
            if hit.get("t"):
                n["title_cn"] = hit["t"]
            n["summary_cn"] = hit["s"]
            applied += 1
        elif n.get("id"):
            pending.append(n)
    if not pending:
        return applied
    try:
        skill = registry.get("panel-news-summary")
    except KeyError:
        return applied
    for off in range(0, len(pending), 15):
        part = pending[off:off + 15]
        feed = [{"id": n["id"], "lang": n.get("lang") or "cn", "title": n["title"],
                 "summary": (n.get("summary") or "")[:300]} for n in part]
        try:
            res = await run_skill_pipeline(skill, {"news_json": _dump(feed)},
                                           STAGE_DIR / "summary" / f"b{off // 15}.json")
        except Exception:
            continue  # 单批失败保留原文摘要
        for n in part:
            it = (res.get("items") or {}).get(n["id"]) or {}
            s_cn = str(it.get("summary_cn") or "").strip()
            t_cn = str(it.get("title_cn") or "").strip()
            if not s_cn or not (60 <= len(s_cn) <= 320):
                continue
            orig_nums = set(re.findall(r"\d+(?:\.\d+)?%?", n["title"] + " " + (n.get("summary") or "")))
            new_nums = set(re.findall(r"\d+(?:\.\d+)?%?", s_cn + " " + t_cn))
            if not new_nums <= orig_nums:
                continue  # 引入原文外数字 → 防编造
            if n.get("lang") == "en" and t_cn and len(t_cn) <= 60:
                n["title_cn"] = t_cn
            n["summary_cn"] = s_cn
            cache[_key(n)] = {"t": n.get("title_cn"), "s": s_cn}
            applied += 1
    atomic_write_json(PANEL_OUT_DIR / _I18N_CACHE, cache)
    return applied
    try:
        skill = registry.get("panel-news-i18n")
    except KeyError:
        return applied
    for off in range(0, len(pending), 20):
        part = pending[off:off + 20]
        feed = [{"id": n["id"], "title": n["title"], "summary": (n.get("summary") or "")[:300]} for n in part]
        try:
            res = await run_skill_pipeline(skill, {"news_json": _dump(feed)},
                                           STAGE_DIR / "i18n" / f"b{off // 20}.json")
        except Exception:
            continue  # 单批失败保留英文原文
        for n in part:
            it = (res.get("items") or {}).get(n["id"]) or {}
            t_cn = str(it.get("title_cn") or "").strip()
            s_cn = str(it.get("summary_cn") or "").strip()
            if not t_cn or len(t_cn) > 60 or (s_cn and len(s_cn) > 180):
                continue
            orig_nums = set(re.findall(r"\d+(?:\.\d+)?%?", n["title"] + " " + (n.get("summary") or "")))
            new_nums = set(re.findall(r"\d+(?:\.\d+)?%?", t_cn + " " + s_cn))
            if not new_nums <= orig_nums:
                continue  # 翻译引入原文外数字 → 防错译，保留英文
            n["title_cn"], n["summary_cn"] = t_cn, (s_cn or None)
            cache[_key(n)] = {"t": t_cn, "s": s_cn or None}
            applied += 1
    atomic_write_json(PANEL_OUT_DIR / _I18N_CACHE, cache)
    return applied


async def _stage_events(pd: dict, news: list[dict]) -> None:
    skill = registry.get("panel-events")
    feed = [{"id": n["id"], "code": n["code"], "name": n["name"], "date": n["time"],
             "title": n["title"], "content": n["summary"][:160]} for n in news]
    # 按标的分批（每批 ≤ 5 只股票，约 60 条新闻），避免单次输出过长被截断
    codes = list(dict.fromkeys(f["code"] for f in feed))
    groups = [set(codes[i:i + 5]) for i in range(0, len(codes), 5)]

    async def one(i: int, g: set):
        part = [f for f in feed if f["code"] in g]
        return await run_skill_pipeline(skill, {"news_json": _dump(part)}, STAGE_DIR / f"events_b{i}.json",
                                        source_numbers=quality.numbers_from_digest(part))

    outs = await asyncio.gather(*(one(i, g) for i, g in enumerate(groups)), return_exceptions=True)
    ok_outs = [o for o in outs if not isinstance(o, Exception)]
    if not ok_outs:
        raise outs[0]
    by_id = {n["id"]: n for n in news}
    items = []
    for it in (x for o in ok_outs for x in (o.get("items") or [])):
        n = by_id.get(it.get("id"))
        if not n:
            continue  # 模型编造的 id 直接丢弃
        items.append({"code": n["code"], "name": n["name"], "date": n["time"],
                      "category": it["category"], "type": it.get("type") or it["category"],
                      "title": it["title"], "desc": it.get("desc") or n["title"], "url": n.get("url"),
                      "news_id": n["id"]})  # 事件与源新闻同一对象：时间线聚类时强制同簇（去重）
    items += _filing_events(pd.get("filings") or {})
    await _attach_event_briefs(pd, items)
    # 已核验的一手公告排在同日转载和媒体事件之前
    items.sort(key=lambda x: (x["date"], 1 if x.get("official") else 0), reverse=True)
    _write("events", {
        "generated_at": pd["generated_at"],
        "source": "港股公告：东方财富公告库转载（非一手，需核验原文）；美股公告：SEC EDGAR 官方链接；媒体资讯：东方财富 / Finnhub → DeepSeek 事件归类；港股财报日期为程序估算，美股为 Finnhub 日历日期",
        "upcoming_earnings": pd["upcoming"],
        "items": items,
    })


# ---------------------------------------------------------------- ③ 信号 / 速览 / 新闻情绪

ANALYSIS_BATCH = 7  # 单次调用覆盖的标的数；20 只一次性输出会超长截断（踩过的坑）


async def _stage_analysis(pd: dict, news: list[dict]) -> None:
    """并行：市场速览 ×1 + 信号分批 ×N + 新闻情绪 ×1。任一信号批失败 → 整体失败保留 last-good。"""
    skill = registry.get("panel-analysis")
    stocks = portfolio.all_stocks()
    today = dt.date.today().isoformat()
    digest_all = [_stock_digest(s, pd, compact=True) for s in stocks]
    titles_all = [{"id": n["id"], "code": n["code"], "date": n["time"], "title": n["title"]} for n in news]

    async def batch(i: int, part: list[dict]):
        codes = {d["code"] for d in part}
        titles = [t for t in titles_all if t["code"] in codes]
        ctx = {"today": today, "digest_json": _dump(part), "news_json": _dump(titles),
               "n_stocks": len(part), "with_note": False}
        return await run_skill_pipeline(skill, ctx, STAGE_DIR / f"analysis_b{i}.json",
                                        source_numbers=quality.numbers_from_digest([part, titles]))

    async def note():
        slim = [{k: d[k] for k in ("code", "name", "price", "pct_today", "position", "latest", "research")} for d in digest_all]
        ctx = {"today": today, "digest_json": _dump(slim), "news_json": _dump(titles_all),
               "n_stocks": 0, "with_note": True}
        return await run_skill_pipeline(skill, ctx, STAGE_DIR / "analysis_note.json",
                                        source_numbers=quality.numbers_from_digest([slim, titles_all]))

    parts = [digest_all[i:i + ANALYSIS_BATCH] for i in range(0, len(digest_all), ANALYSIS_BATCH)]
    results = await asyncio.gather(note(), *(batch(i, p) for i, p in enumerate(parts)), return_exceptions=True)
    note_res, batch_res = results[0], results[1:]
    for r in batch_res:
        if isinstance(r, Exception):
            raise r
    valid = {s["code"] for s in stocks}
    name_of = {s["code"]: s["name"] for s in stocks}
    signals = []
    for r in batch_res:
        for x in r.get("signals") or []:
            if x.get("code") in valid:
                x["name"] = name_of.get(x["code"], "")
                x.setdefault("conditions", [])
                signals.append(x)
    order = {s["code"]: i for i, s in enumerate(stocks)}
    signals.sort(key=lambda x: order.get(x["code"], 999))
    prev = read_json(PANEL_OUT_DIR / "analysis.json", {}) or {}
    market_note = note_res.get("market_note") if not isinstance(note_res, Exception) else prev.get("market_note", "")
    res = {"market_note": market_note}
    news_out = [{"code": n["code"], "name": n["name"], "title": n["title"], "source": n["source"],
                 "time": n["time"], "sentiment": n.get("sentiment", "中性"),
                 "sentiment_basis": n.get("sentiment_basis"), "sentiment_conf": n.get("sentiment_conf"), "url": n["url"],
                 "summary": n["summary"],
                 **({"lang": n["lang"]} if n.get("lang") else {}),
                 **({"title_cn": n["title_cn"]} if n.get("title_cn") else {}),
                 **({"summary_cn": n["summary_cn"]} if n.get("summary_cn") else {})} for n in news]
    _write("analysis", {
        "generated_at": pd["generated_at"],
        "sources": [
            f"腾讯行情（{pd['generated_at']} 实时快照）",
            "东方财富财务指标 / 经济通券商评级（via AKShare）",
            f"东方财富资讯搜索 + Finnhub（美股）共 {len(news)} 条（近 3 天，经投资者价值筛选，已合并同类快讯与跨媒体重复）",
            "信号与速览由 DeepSeek 生成，经 Schema 校验与文本数字对账",
        ],
        "market_note": res.get("market_note", ""),
        "signals": signals,
        "news": news_out,
        "news_note": "summary 为原文摘录（≤300 字）；每股每日 ≤3 条、例行快讯同族同日 1 条；点名但主角非本公司的稿件已剔除。"
                     "情绪默认中性：仅当含硬信息（业绩/机构评级/资本动作/订单/监管/负面事件/推迟）、置信度 ≥85%、且两次独立判断一致时才标利好/利空",
    })


# ---------------------------------------------------------------- ④ 主题

async def _stage_themes(pd: dict) -> None:
    skill = registry.get("panel-themes")
    stocks = portfolio.all_stocks()
    digest = []
    for s in stocks:
        d = _stock_digest(s, pd, compact=True)
        digest.append({k: d[k] for k in ("code", "name", "sector", "position", "latest", "research")})
    res = await run_skill_pipeline(skill, {"today": dt.date.today().isoformat(), "digest_json": _dump(digest)},
                                   STAGE_DIR / "themes.json", source_numbers=quality.numbers_from_digest(digest))
    valid = {s["code"] for s in stocks}
    seen: set[str] = set()
    items = []
    for t in res.get("items") or []:
        members = [c for c in t.get("members") or [] if c in valid and c not in seen]
        if len(members) < 2:
            continue
        seen.update(members)
        items.append({"id": t["id"], "name": t["name"], "thesis": t["thesis"],
                      "members": members, "updated": dt.date.today().isoformat()})
    _write("themes", {"generated_at": pd["generated_at"],
                      "sources": ["组合配置 data/config/holdings.json", "主题聚类与逻辑由 DeepSeek 生成"],
                      "items": items})
    if not (PANEL_OUT_DIR / "trends.json").exists():
        _write("trends", {"generated_at": pd["generated_at"], "sources": [], "items": []})


# ---------------------------------------------------------------- ⑤ 一页纸

def _blocks(kind: str, v: Any) -> list[dict]:
    if kind == "p":
        return [{"type": "p", "text": str(v or "")}]
    if kind == "bullets":
        return [{"type": "bullets", "items": [str(x) for x in (v or [])]}]
    if kind == "lead_bullets":
        return [{"type": "bullets", "items": [{"lead": x.get("lead", ""), "text": x.get("text", "")} for x in (v or []) if isinstance(x, dict)]}]
    if kind == "table":
        rows = [[str(c or "") for c in (r or [])][:3] for r in (v or []) if isinstance(r, list)]
        return [{"type": "table", "cols": ["时间", "事件", "关注点"], "rows": rows}]
    if kind == "bullbear":
        v = v if isinstance(v, dict) else {}
        return [{"type": "bullbear", "bull": [str(x) for x in (v.get("bull") or [])][:4], "bear": [str(x) for x in (v.get("bear") or [])][:4]}]
    return []


def _scrub_blocks(blocks: list[dict]) -> list[dict]:
    for b in blocks:
        if b.get("type") == "p":
            b["text"] = _scrub_private(b["text"])
        elif b.get("type") == "bullets":
            b["items"] = [x for x in ((_scrub_private(i) if isinstance(i, str) else {**i, "text": _scrub_private(i.get("text", ""))}) for i in b["items"])
                          if (x if isinstance(x, str) else x.get("text"))]
        elif b.get("type") == "bullbear":
            b["bull"] = [y for y in (_scrub_private(x) for x in b["bull"]) if y]
            b["bear"] = [y for y in (_scrub_private(x) for x in b["bear"]) if y]
        elif b.get("type") == "table":
            b["rows"] = [r for r in b["rows"] if not any(_OP_PRIVATE.search(c) for c in r)]
    return blocks


async def _onepager_one(s: dict, pd: dict, events: list[dict]) -> tuple[str, dict | None, str | None]:
    code = s["code"]
    skill = registry.get("panel-onepager")
    d = _stock_digest(s, pd, compact=False)
    d["hints"] = _onepager_hints(d)
    for k in ("position", "price", "pct_today", "market_cap_yi", "valuation"):  # 持仓与会每天变的数字不进模型
        d.pop(k, None)
    if d.get("research"):
        d["research"] = {k: v for k, v in d["research"].items() if k in ("rating_latest", "next_report", "target_count")}
    d["events"] = [{"date": e["date"], "title": e["title"], "desc": e["desc"]} for e in events if e["code"] == code][:8]
    d["research_reports"] = ((pd["research"]["items"].get(code) or {}).get("reports") or [])[:4]
    ctx = {"code": code, "name": s["name"], "today": dt.date.today().isoformat(), "digest_json": _dump(d)}
    try:
        res = await run_skill_pipeline(skill, ctx, STAGE_DIR / "onepager" / f"{code}.json",
                                       source_numbers=quality.numbers_from_digest(d))
    except Exception as e:
        return code, None, str(e)[:200]
    today = dt.date.today().isoformat()
    sections = []
    for sid, num, title, lead, embed, kind in SECTIONS:
        sec: dict[str, Any] = {"id": sid, "num": num, "title": title, "updated": today,
                               "blocks": _scrub_blocks(_blocks(kind, res.get(sid))) if kind else []}
        if lead:
            sec["lead"] = lead
        if embed:
            sec["embed"] = embed
        sections.append(sec)
    return code, {"name": s["name"], "updated_at": today, "sections": sections}, None


async def _stage_onepager(pd: dict, only_codes: set[str] | None = None) -> dict:
    (STAGE_DIR / "onepager").mkdir(parents=True, exist_ok=True)
    events = (filings.normalize_panel_sources("events", read_json(PANEL_OUT_DIR / "events.json", {}) or {})).get("items") or []
    sem = asyncio.Semaphore(5)
    stocks = portfolio.all_stocks()
    if only_codes is not None:
        stocks = [s for s in stocks if s.get("code") in only_codes]

    async def guarded(s):
        async with sem:
            return await _onepager_one(s, pd, events)

    results = await asyncio.gather(*(guarded(s) for s in stocks))
    prev = read_json(PANEL_OUT_DIR / "onepager.json", {}) or {}
    items = dict(prev.get("items") or {})  # 单股失败保留该股上一版（last-good 粒度到个股）
    errors = {}
    for code, item, err in results:
        if item:
            items[code] = item
        else:
            errors[code] = err
    _write("onepager", {
        "_comment": "公司一页纸——低频更新的深度分析文档（周更或重大事件/财报后更新），由 harness panel-onepager 生成",
        "generated_at": pd["generated_at"],
        "sources": ["东方财富财务指标 / 经济通券商评级（via AKShare）", "东方财富资讯 / Finnhub 新闻", "腾讯行情",
                    "正文由 DeepSeek 生成：数字经文本对账，定性内容为公开常识，请结合原始数据判断"],
        "items": items,
    })
    return errors


# ---------------------------------------------------------------- 入口

async def _attach_filings(pd: dict) -> None:
    """公告目录：挂到 pd（事件流/一页纸摘要用）并填充财报 Tab 的「相关公告」。失败不影响主流程。"""
    try:
        fil = await filings.fetch_filings(portfolio.all_stocks())
    except Exception:
        fil = {}
    pd["filings"] = fil
    for code, e in pd["earnings"]["items"].items():
        e["announcements"] = filings.earnings_announcements(fil.get(code) or [])
    if fil:
        pd["earnings"]["sources"] = list(pd["earnings"]["sources"]) + ["相关公告：港股东方财富公告库转载（非一手，需核验原文）/ 美股 SEC EDGAR"]


async def _prepare(pd: dict) -> tuple[list[dict], dict]:
    await _attach_filings(pd)
    news = pd["news"]
    for i, n in enumerate(news):
        n["id"] = f"n{i}"
    try:
        news, rel = await _stage_relevance(news)
    except Exception as e:
        rel = {"error": str(e)[:200]}
    try:
        rel["summary"] = await _stage_summary(news)  # 全量新闻中文摘要（100-300字，缓存持久）
    except Exception as e:
        rel["summary"] = f"FAIL: {str(e)[:120]}"
    pd["news"] = news
    return news, rel


async def run(full: bool = False) -> dict:
    t0 = time.time()
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    pd = await panel_data.build_program_data()
    news, rel = await _prepare(pd)
    _write("fundamentals", pd["fundamentals"])
    _write("earnings", pd["earnings"])
    _write("research", pd["research"])
    # SEC 官方标准科目独立更新；失败只影响三表摘录，不阻断已有面板产物。
    sec_task = asyncio.create_task(panel_data.refresh_sec_statements(portfolio.all_stocks()))
    bus.publish("panel_data", {"stage": "program", "stocks": len(pd["fundamentals"]["items"])})

    report: dict[str, Any] = {"program_s": pd["elapsed_s"], "errors": pd["errors"],
                              "filings": sum(len(v) for v in (pd.get("filings") or {}).values()),
                              "news_kept": len(news), "relevance": {k: v for k, v in rel.items() if k != "samples"}}
    stages = [("events", _stage_events(pd, news)), ("analysis", _stage_analysis(pd, news)), ("themes", _stage_themes(pd))]
    outs = await asyncio.gather(*(c for _, c in stages), return_exceptions=True)
    for (name, _), o in zip(stages, outs):
        report[name] = "ok" if not isinstance(o, Exception) else f"FAIL: {str(o)[:200]}"
    try:
        report["sec_statements"] = await sec_task
    except Exception as exc:
        report["sec_statements"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}", "updated": 0}
    prev_onepager = read_json(PANEL_OUT_DIR / "onepager.json", {}) or {}
    missing_op = {s["code"] for s in portfolio.all_stocks()
                  if s.get("code") and s["code"] not in (prev_onepager.get("items") or {})}
    if full or missing_op:
        try:
            report["onepager_errors"] = await _stage_onepager(pd, None if full else missing_op)
            if not full and missing_op:
                report["onepager_backfill"] = sorted(missing_op)
        except Exception as e:
            report["onepager"] = f"FAIL: {e}"
    report["elapsed_s"] = round(time.time() - t0, 1)
    ok = all(report.get(k) == "ok" for k in ("events", "analysis", "themes"))
    log_run("panel-build" + (":full" if full else ""), "ok" if ok else "partial", json.dumps(report, ensure_ascii=False)[:600])
    bus.publish("job_done", {"job": "panel-build", "report": report})
    if not ok:
        raise JobFailed(f"部分阶段失败：{report}")
    return report


async def run_full() -> dict:
    return await run(full=True)


async def run_onepager_only() -> dict:
    pd = await panel_data.build_program_data()
    await _attach_filings(pd)
    errs = await _stage_onepager(pd)
    log_run("panel-onepager", "ok" if not errs else "partial", json.dumps(errs, ensure_ascii=False)[:400])
    return {"errors": errs}


# ---------------------------------------------------------------- 单股刷新（2026-10-16：新增股票后立即补齐该股全部信息）

def _mini_pd(code: str) -> dict:
    """从既有面板 JSON 组装的精简 pd（供 _stock_digest / 一页纸用）。"""
    def rd(n):
        return read_json(PANEL_OUT_DIR / f"{n}.json", {}) or {}
    quotes = {}
    return {"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "quotes": quotes,
            "fundamentals": rd("fundamentals"), "earnings": rd("earnings"), "research": rd("research"),
            "upcoming": (rd("events").get("upcoming_earnings") or []),
            "filings": {}, "news": []}


async def refresh_llm_one(code: str) -> dict:
    """单股 LLM 内容：新闻情绪/信号/事件/一页纸/大师分析；全部只改该股条目，不动其他股票。"""
    from .. import filings as filings_mod
    from .. import panel_api, valuation
    st = next((s for s in portfolio.all_stocks() if s.get("code") == code), None)
    if not st:
        return {"ok": False, "error": "unknown code"}
    steps: dict[str, Any] = {}
    today = dt.date.today().isoformat()
    pd = _mini_pd(code)

    # 公告（单股）
    try:
        fil = await filings_mod.fetch_filings([st])
        if fil:
            e0 = read_json(PANEL_OUT_DIR / "earnings.json", {}) or {}
            if code in (e0.get("items") or {}):
                e0["items"][code]["announcements"] = filings_mod.earnings_announcements(fil.get(code) or [])
                _write("earnings", e0)
        pd["filings"] = fil
        steps["filings"] = "ok"
    except Exception as e:
        steps["filings"] = f"fail: {str(e)[:80]}"

    # 新闻情绪 + 信号（该股条目替换）
    try:
        ana = read_json(PANEL_OUT_DIR / "analysis.json", {}) or {}
        mine = [n for n in (ana.get("news") or []) if n.get("code") == code]
        news, _ = await _stage_relevance(mine) if mine else (mine, {})
        d = _stock_digest(st, pd, compact=True)
        skill = registry.get("panel-analysis")
        titles = [{"id": n["id"], "code": code, "date": n["time"], "title": n["title"]} for n in news]
        res = await run_skill_pipeline(skill, {"today": today, "digest_json": _dump([d]), "news_json": _dump(titles),
                                               "n_stocks": 1, "with_note": False},
                                       STAGE_DIR / f"one_{code}.json",
                                       source_numbers=quality.numbers_from_digest([[d], titles]))
        sig = [x for x in (res.get("signals") or []) if x.get("code") == code]
        ana = read_json(PANEL_OUT_DIR / "analysis.json", {}) or {}
        ana["news"] = [n for n in (ana.get("news") or []) if n.get("code") != code] + news
        ana["signals"] = [x for x in (ana.get("signals") or []) if x.get("code") != code] + sig
        _write("analysis", ana)
        steps["analysis"] = "ok"
    except Exception as e:
        steps["analysis"] = f"fail: {str(e)[:80]}"

    # 事件（该股条目替换，只处理该股近三天新闻）
    try:
        ana = read_json(PANEL_OUT_DIR / "analysis.json", {}) or {}
        feed = [{"id": n["id"], "code": code, "name": n["name"], "date": n["time"],
                 "title": n["title"], "content": n["summary"][:160]}
                for n in (ana.get("news") or []) if n.get("code") == code]
        if feed:
            skill = registry.get("panel-events")
            res = await run_skill_pipeline(skill, {"news_json": _dump(feed)}, STAGE_DIR / f"events_one_{code}.json",
                                           source_numbers=quality.numbers_from_digest(feed))
            by_id = {f["id"]: f for f in feed}
            new_items = [{"code": code, "name": by_id[it["id"]]["name"], "date": by_id[it["id"]]["date"],
                          "category": it["category"], "type": it.get("type") or it["category"],
                          "title": it["title"], "desc": it.get("desc") or by_id[it["id"]]["title"],
                          "url": by_id[it["id"]].get("url"), "news_id": it["id"]}
                         for it in (res.get("items") or []) if it.get("id") in by_id]
            new_items += _filing_events(pd.get("filings") or {})
            ev = read_json(PANEL_OUT_DIR / "events.json", {}) or {}
            ev["items"] = [x for x in (ev.get("items") or []) if x.get("code") != code] + new_items
            ev["items"].sort(key=lambda x: (x["date"], 1 if x.get("official") else 0), reverse=True)
            _write("events", ev)
        steps["events"] = "ok"
    except Exception as e:
        steps["events"] = f"fail: {str(e)[:80]}"

    # 一页纸（该股）
    try:
        pd2 = _mini_pd(code)
        await _attach_filings(pd2)
        errs = await _stage_onepager(pd2, {code})
        steps["onepager"] = "ok" if not errs else f"fail: {errs}"
    except Exception as e:
        steps["onepager"] = f"fail: {str(e)[:80]}"

    # 大师分析（该股 × 个股级大师）
    try:
        personas = [p_["id"] for p_ in portfolio.load_personas() if (p_.get("scope") or "stock") == "stock"]
        sem = asyncio.Semaphore(3)
        async def one(pid):
            async with sem:
                return await panel_api.generate_expert(code, pid, True)
        rs = await asyncio.gather(*(one(pid) for pid in personas), return_exceptions=True)
        steps["experts"] = f"{sum(1 for r in rs if isinstance(r, dict) and r.get('ok'))}/{len(personas)}"
    except Exception as e:
        steps["experts"] = f"fail: {str(e)[:80]}"

    # 估值（三把尺子，存结果）
    try:
        vd = await asyncio.to_thread(valuation.view, code, False)
        if vd.get("ok") or vd.get("error") == "insufficient":
            valuation.store_result(code, vd)
        steps["valuation"] = vd.get("verdict") or vd.get("reason") or "ok"
    except Exception as e:
        steps["valuation"] = f"fail: {str(e)[:80]}"

    # 价格信号 v2（该股，合并进缓存）
    try:
        from .. import signals_service
        d2 = await signals_service.refresh_signal_one(code)
        steps["signals"] = "ok" if d2 and d2.get("items") else "empty"
    except Exception as e:
        steps["signals"] = f"fail: {str(e)[:80]}"

    from ..store import log_run
    log_run("refresh-one:" + code, "ok" if all(str(v).startswith(("ok", "/",)) or "/" in str(v) for v in steps.values()) else "partial",
            json.dumps(steps, ensure_ascii=False)[:300])
    return {"ok": True, "steps": steps}
