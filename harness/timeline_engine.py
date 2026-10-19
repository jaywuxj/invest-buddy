"""动态时间线的服务端引擎（移植 public/timeline.js 的 collect/cluster/ruleScore）。

用途：事件 LLM 评分（events_score.py）需要与前端完全一致的簇 ID
（kind:date:title前16字），因此聚类与规则评分逻辑必须与前端逐行同口径。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

KIND_RANK = {"official": 3, "event": 2, "media": 1}
MERGE_CATS = {"财务披露", "回购", "分红送配", "监管处罚"}
PERIODIC_TYPE = {"年报", "中报", "季报", "业绩公告", "10-K", "10-Q"}
PERIODIC_TITLE = re.compile(
    r"年度报告|中期报告|半年度报告|季度报告|业绩公告|中期业绩|全年业绩|季度业绩|止三个月|止六个月|止九个月|止十二个月|止年度|10-K|10-Q")
CAT_RULES = [
    ("财务披露", re.compile(r"业绩|财报|营收|净利|季报|中报|年报|中期报告|半年报|盈利预警|盈喜|盈警|指引|EPS|收入(同比|增长|下滑|下降)")),
    ("回购", re.compile(r"回购")),
    ("分红送配", re.compile(r"分红|派息|股息|送股|派发")),
    ("监管处罚", re.compile(r"罚|监管|调查|诉讼|立案|约谈|违规|反垄断|制裁|禁令")),
    ("股权变动", re.compile(r"增持|减持|持股|举牌|配售|大股东|解禁")),
    ("风险提示", re.compile(r"风险|警示|停牌|退市|违约")),
]
CAT_BASE = {"监管处罚": 4, "风险提示": 4, "财务披露": 3, "回购": 3, "分红送配": 2, "股权变动": 2, "公司经营": 2}
KEY_UP = re.compile(r"超预期|不及预期|低于预期|高于预期|指引|上调|下调|收购|并购|重组|制裁|禁令|反垄断|辞任|辞职|停牌|退市|私有化|分拆|要约|大额|巨额|禁售|出口管制|关税")
BIG_AMT = re.compile(r"(\d{3,}|\d+(\.\d+)?万)\s*亿|罚[^，。]{0,6}\d+(\.\d+)?\s*亿")
BROKER = re.compile(r"评级|目标价|观点|看好|首予|首次覆盖|研报|证券：|银行：|资本：|花旗|高盛|摩根|大摩|小摩|瑞银|美银|中金|Cowen|Seaport|Jefferies|Bernstein")
KEY_STRONG = re.compile(r"暴跌|暴涨|重挫|大涨|大跌|创新高|创新低|史上|重磅|紧急|突发|腰斩|翻倍")
ROUTINE = re.compile(r"董事会会议|举行董事会|月报表|翌日披露|证券变动月报|股东周年大会通告|通函|委任代表表格|更换.*代理")
HORIZON = {"财务披露": "中期", "回购": "短期", "分红送配": "长期", "监管处罚": "中期", "股权变动": "短期", "风险提示": "短期", "公司经营": "中期"}
CHAIN = {
    "财务披露": "业绩 → 盈利预期修正 → 估值重定价",
    "回购": "回购 → 流通股减少/管理层信号 → EPS 与估值支撑",
    "分红送配": "分红 → 股东回报率 → 长线资金偏好",
    "监管处罚": "监管 → 合规成本/业务受限 → 盈利与风险溢价",
    "股权变动": "股东增减持 → 内部人信号/供给压力 → 短期资金面",
    "风险提示": "风险事件 → 不确定性上升 → 风险溢价抬升",
    "公司经营": "经营动作 → 收入/成本预期 → 中期基本面",
}
_OPPOSITE = [(re.compile("增持"), re.compile("减持")), (re.compile("上调"), re.compile("下调")), (re.compile("买入"), re.compile("卖出"))]


def _day(s: Any) -> str:
    return str(s or "")[:10]


def _dnum(s: str) -> float:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(s or ""))
    if not m:
        return float("nan")
    return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc).timestamp() / 86400


def _infer_cat(text: str) -> str:
    for name, pat in CAT_RULES:
        if pat.search(text):
            return name
    return "公司经营"


def _infer_senti(cat: str, text: str) -> str:
    if re.search(r"减持|处罚|罚款|调查|诉讼|下调|亏损|风险|暴跌|终止", text):
        return "利空"
    if cat in ("回购", "分红送配") or re.search(r"增持|上调|超预期|创新高", text):
        return "利好"
    if cat in ("监管处罚", "风险提示"):
        return "利空"
    return "中性"


def _norm(t: str, name: str) -> str:
    s = str(t or "")
    if name:
        s = s.replace(re.sub(r"-[A-Z]+$", "", str(name)), "")
    return re.sub(r"[\s，。、：:；;“”\"'（）()【】\[\]·\-—|,.!?！？]", "", s).lower()


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _dice(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def _opposite(a: str, b: str) -> bool:
    return any((p.search(a) and n.search(b)) or (n.search(a) and p.search(b)) for p, n in _OPPOSITE)


def collect(code: str, src: dict) -> list[dict]:
    out = []
    for n in (src.get("news") or []):
        if n.get("code") != code:
            continue
        txt = (n.get("title") or "") + " " + (n.get("summary") or "")
        out.append({"kind": "media", "code": code, "name": n.get("name"), "date": _day(n.get("time")),
                    "nid": n.get("id") or "",
                    "title": n.get("title") or "", "body": n.get("summary") or "",
                    "source": n.get("source") or "", "url": n.get("url") or "",
                    "sentiment": n.get("sentiment") or _infer_senti(_infer_cat(n.get("title") or ""), txt),
                    "category": _infer_cat(n.get("title") or ""), "type": ""})
    ev = src.get("events") or {}
    for e in (ev.get("items") or []):
        if e.get("code") != code:
            continue
        cat = e.get("category") or _infer_cat((e.get("title") or "") + (e.get("type") or ""))
        out.append({"kind": "event", "code": code, "name": e.get("name"), "date": _day(e.get("date")),
                    "nid": e.get("news_id") or "",
                    "title": e.get("title") or "", "body": e.get("brief") or e.get("desc") or "",
                    "source": "公司披露", "url": e.get("url") or "",
                    "sentiment": _infer_senti(cat, (e.get("title") or "") + " " + (e.get("type") or "")),
                    "category": cat, "type": e.get("type") or ""})
    er = ((src.get("earnings") or {}).get("items") or {}).get(code) or {}
    for a in (er.get("announcements") or []):
        if a.get("type") in PERIODIC_TYPE or PERIODIC_TITLE.search(a.get("title") or ""):
            continue
        out.append({"kind": "official", "code": code, "name": er.get("name"), "date": _day(a.get("date")),
                    "title": a.get("title") or "", "body": "",
                    "source": "交易所公告" + (" · " + a["type"] if a.get("type") else ""),
                    "url": a.get("url") or "", "sentiment": "中性",
                    "category": _infer_cat(a.get("title") or ""), "type": a.get("type") or ""})
    return [x for x in out if x["date"]]


def cluster(items: list[dict], name: str) -> list[dict]:
    for x in items:
        x["_bg"] = _bigrams(_norm(x["title"], name))
        x["_d"] = _dnum(x["date"])
    items.sort(key=lambda x: (-(x["_d"] if x["_d"] == x["_d"] else 0), -KIND_RANK[x["kind"]]))
    groups: list[list[dict]] = []
    for x in items:
        hit = None
        for g in groups:
            found = False
            for y in g:
                if x.get("nid") and x["nid"] == y.get("nid"):
                    hit = g  # 事件与其源新闻同一对象，强制同簇（去重）
                    x["_sim"] = 1.0
                    found = True
                    break
                if abs(x["_d"] - y["_d"]) > 2 or _opposite(x["title"], y["title"]):
                    continue
                sim = _dice(x["_bg"], y["_bg"])
                if sim >= 0.5 or (x["category"] == y["category"] and x["category"] in MERGE_CATS and x["kind"] != y["kind"]):
                    hit = g
                    x["_sim"] = sim
                    found = True
                    break
            if found:
                break
        if hit is not None:
            hit.append(x)
        else:
            groups.append([x])
    out = []
    for g in groups:
        g.sort(key=lambda x: (-KIND_RANK[x["kind"]], -(x["_d"] if x["_d"] == x["_d"] else 0)))
        main = g[0]
        sent = main["sentiment"]
        if main["kind"] != "media":
            cnt = {"利好": 0, "利空": 0}
            for x in g:
                if x["kind"] == "media" and x["sentiment"] in cnt:
                    cnt[x["sentiment"]] += 1
            if sent == "中性" and cnt["利好"] != cnt["利空"]:
                sent = "利好" if cnt["利好"] > cnt["利空"] else "利空"
        kinds = list({x["kind"] for x in g})
        latest = max(x["date"] for x in g)
        clean = lambda x: {k: v for k, v in x.items() if not k.startswith("_")}
        out.append({"id": f"{main['kind']}:{main['date']}:{main['title'][:16]}", "date": latest,
                    "kind": main["kind"], "kinds": kinds, "category": main["category"],
                    "sentiment": sent, "main": clean(main), "related": [clean(x) for x in g[1:]],
                    "size": len(g)})
    out.sort(key=lambda c: (c["date"], KIND_RANK[c["kind"]]), reverse=True)
    return out


def rule_score(c: dict) -> dict:
    txt = " ".join([c["main"].get("title") or "", c["main"].get("body") or ""] + [r.get("title") or "" for r in c["related"]])
    imp = CAT_BASE.get(c["category"], 2)
    why = [f"{c['category']} 基础 {imp}"]
    if c["size"] >= 3:
        imp += 1
        why.append(f"多源报道 ×{c['size']}")
    k = KEY_UP.search(txt)
    if k:
        imp += 1
        why.append(f"关键词「{k[0]}」")
    s = KEY_STRONG.search(txt)
    if s:
        imp += 1
        why.append(f"强烈措辞「{s[0]}」")
    a = BIG_AMT.search(txt) if (c["kind"] != "media" or re.search(r"监管处罚|回购|风险提示|股权变动", c["category"])) else None
    if a:
        imp += 1
        why.append(f"大额「{a[0]}」")
    if c["kind"] == "media" and c["size"] < 3 and BROKER.search(c["main"].get("title") or "") and imp > 3:
        imp = 3
        why.append("单家券商观点封顶 3")
    if "official" in c["kinds"] and imp < 3:
        imp = 3
        why.append("交易所公告保底 3")
    if ROUTINE.search(c["main"].get("title") or ""):
        imp = min(imp, 2)
        why.append("例行程序性公告封顶 2")
    imp = max(1, min(5, imp))
    direction = "正面" if c["sentiment"] == "利好" else "负面" if c["sentiment"] == "利空" else "中性"
    return {"imp": imp, "direction": direction, "horizon": HORIZON.get(c["category"], "中期"),
            "chain": CHAIN.get(c["category"], CHAIN["公司经营"]), "why": "；".join(why), "src": "rule"}


def apply_score(c: dict, scores: dict | None) -> dict:
    r = rule_score(c)
    l = (scores or {}).get(c["id"])
    c["score"] = r
    if l and l.get("imp"):
        c["score"] = {"imp": max(1, min(5, int(l["imp"]))), "direction": l.get("direction") or r["direction"],
                      "horizon": l.get("horizon") or r["horizon"], "chain": l.get("chain") or r["chain"],
                      "why": l.get("why") or r["why"], "src": l.get("src") or "llm",
                      "rule_imp": r["imp"], "scored_at": l.get("scored_at") or ""}
    c["imp"] = c["score"]["imp"]
    return c


def build(code: str, src: dict) -> dict:
    """与前端 WBTimeline.build 同口径：{code, today, upcoming, items, stats}"""
    today = src.get("today") or datetime.now(timezone.utc).date().isoformat()
    raw = collect(code, src)
    name = raw[0].get("name") or "" if raw else ""
    cl = [apply_score(c, src.get("scores")) for c in cluster(raw, name)]
    by_kind: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    by_senti: dict[str, int] = {}
    by_imp: dict[int, int] = {}
    for x in raw:
        by_kind[x["kind"]] = by_kind.get(x["kind"], 0) + 1
    for c in cl:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
        by_senti[c["sentiment"]] = by_senti.get(c["sentiment"], 0) + 1
        by_imp[c["imp"]] = by_imp.get(c["imp"], 0) + 1
    upcoming = []
    t0 = _dnum(today)
    for e in ((src.get("events") or {}).get("upcoming_earnings") or []):
        if e.get("code") != code:
            continue
        upcoming.append({"type": "earnings", "date": _day(e.get("date")),
                         "title": "财报披露" + (f"（{e['period']}）" if e.get("period") else ""),
                         "note": e.get("note") or ""})
    th = ((src.get("theses") or {}).get("items") or {}).get(code)
    if th and th.get("review_date"):
        upcoming.append({"type": "thesis", "date": _day(th["review_date"]),
                         "title": "复查我的判断", "note": th.get("summary") or ""})
    for u in upcoming:
        u["days"] = None if t0 != t0 else int(_dnum(u["date"]) - t0)
    upcoming = sorted((u for u in upcoming if u["days"] is None or -3 <= u["days"] <= 90),
                      key=lambda u: u["date"])
    return {"code": code, "today": today, "upcoming": upcoming, "items": cl,
            "stats": {"raw": len(raw), "clusters": len(cl), "merged": len(raw) - len(cl),
                      "byKind": by_kind, "byCat": by_cat, "bySenti": by_senti, "byImp": by_imp,
                      "high": sum(1 for c in cl if c["imp"] >= 4)}}
