/*
 * 动态时间线（新闻 + 事件 + 交易所公告 合并）—— 前后端共用（UMD，ES5 only）
 *   浏览器：window.WBTimeline.build(code, src)
 *   Node  ：require("./public/timeline.js").build(code, src)
 * src = { news: analysis.news, events: events.json, earnings: earnings.json, theses: theses.json, today: "YYYY-MM-DD" }
 *
 * 口径：
 *   kind   official=交易所公告（earnings.announcements 中的非定期报告；年报/中报/季报/业绩公告已在「财报」Tab，不进动态）
 *          event   =披露事件（events.items：增减持/分红/回购/监管等结构化披露）
 *          media   =媒体报道（analysis.news）
 *   聚类   同一标的、日期相差 ≤2 天，且满足其一：①标题字符 bigram Dice ≥ 0.5；
 *          ②同类别且类别属于「财务披露/回购/分红送配/监管处罚」且来源 kind 不同（媒体对同一公告的报道）
 *   主条   聚类内按 official > event > media 取主条，其余折叠为「相关报道」
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.WBTimeline = factory();
})(this, function () {
  var KIND_RANK = { official: 3, event: 2, media: 1 };
  var KIND_LABEL = { official: "公告", event: "披露", media: "媒体" };
  var MERGE_CATS = { "财务披露": 1, "回购": 1, "分红送配": 1, "监管处罚": 1 };
  /* 定期报告类公告：类型或标题命中即视为与「财报」Tab 重复 */
  var PERIODIC_TYPE = { "年报": 1, "中报": 1, "季报": 1, "业绩公告": 1, "10-K": 1, "10-Q": 1 };
  var PERIODIC_TITLE = /年度报告|中期报告|半年度报告|季度报告|业绩公告|中期业绩|全年业绩|季度业绩|止三个月|止六个月|止九个月|止十二个月|止年度|10-K|10-Q/;
  var CAT_RULES = [
    /* 「收入」单独出现太宽（如「碳积分交易收入预估」属于经营），改为业绩语境词 */
    ["财务披露", /业绩|财报|营收|净利|季报|中报|年报|中期报告|半年报|盈利预警|盈喜|盈警|指引|EPS|收入(同比|增长|下滑|下降)/],
    ["回购", /回购/],
    ["分红送配", /分红|派息|股息|送股|派发/],
    ["监管处罚", /罚|监管|调查|诉讼|立案|约谈|违规|反垄断|制裁|禁令/],
    ["股权变动", /增持|减持|持股|举牌|配售|大股东|解禁/],
    ["风险提示", /风险|警示|停牌|退市|违约/]
  ];

  function day(s) { return String(s || "").slice(0, 10); }
  function dnum(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ""));
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) / 86400000 : NaN;
  }
  function inferCat(text) {
    for (var i = 0; i < CAT_RULES.length; i++) if (CAT_RULES[i][1].test(text)) return CAT_RULES[i][0];
    return "公司经营";
  }
  function inferSenti(cat, text) {
    if (/减持|处罚|罚款|调查|诉讼|下调|亏损|风险|暴跌|终止/.test(text)) return "利空";
    if (cat === "回购" || cat === "分红送配" || /增持|上调|超预期|创新高/.test(text)) return "利好";
    if (cat === "监管处罚" || cat === "风险提示") return "利空";
    return "中性";
  }
  function norm(t, name) {
    var s = String(t || "");
    if (name) s = s.split(String(name).replace(/-[A-Z]+$/, "")).join("");
    return s.replace(/[\s，。、：:；;“”"'（）()【】\[\]·\-—|,.!?！？]/g, "").toLowerCase();
  }
  function bigrams(s) {
    var o = {}, n = 0;
    for (var i = 0; i < s.length - 1; i++) { var g = s.substr(i, 2); if (!o[g]) { o[g] = 1; n++; } }
    return { set: o, n: n };
  }
  function dice(a, b) {
    if (!a.n || !b.n) return 0;
    var hit = 0;
    for (var g in a.set) if (b.set[g]) hit++;
    return (2 * hit) / (a.n + b.n);
  }

  /* 方向相反的同类披露（增持 vs 减持、上调 vs 下调）不聚类 */
  function opposite(a, b) {
    var P = [[/增持/, /减持/], [/上调/, /下调/], [/买入/, /卖出/]];
    for (var i = 0; i < P.length; i++) {
      if ((P[i][0].test(a) && P[i][1].test(b)) || (P[i][1].test(a) && P[i][0].test(b))) return true;
    }
    return false;
  }

  function collect(code, src) {
    var out = [];
    (src.news || []).forEach(function (n) {
      if (n.code !== code) return;
      var txt = (n.title || "") + " " + (n.summary || "");
      out.push({ kind: "media", code: code, name: n.name, date: day(n.time), title: n.title || "", body: n.summary || "", nid: n.id || "",
        title_cn: n.title_cn || "", body_cn: n.summary_cn || "",
        source: n.source || "", url: n.url || "", sentiment: n.sentiment || inferSenti(inferCat(n.title || ""), txt),
        category: inferCat(n.title || ""), type: "" });
    });
    var ev = src.events || {};
    (ev.items || []).forEach(function (e) {
      if (e.code !== code) return;
      var cat = e.category || inferCat((e.title || "") + (e.type || ""));
      out.push({ kind: "event", code: code, name: e.name, date: day(e.date), title: e.title || "", body: e.brief || e.desc || "", nid: e.news_id || "",
        source: "公司披露", url: e.url || "", sentiment: inferSenti(cat, (e.title || "") + " " + (e.type || "")),
        category: cat, type: e.type || "" });
    });
    var er = src.earnings && src.earnings.items ? src.earnings.items[code] : null;
    /* 定期报告（年报/中报/季报/业绩公告）不进动态：「财报」Tab 已有数字、对比与「财报原文」链接，避免重复；
       其余交易所公告（若数据源将来提供）照常进入 */
    ((er && er.announcements) || []).forEach(function (a) {
      if (PERIODIC_TYPE[a.type] || PERIODIC_TITLE.test(a.title || "")) return;
      out.push({ kind: "official", code: code, name: er.name, date: day(a.date), title: a.title || "", body: a.brief || "",
        source: "交易所公告" + (a.type ? " · " + a.type : ""), url: a.url || "", sentiment: "中性",
        category: inferCat(a.title || ""), type: a.type || "" });
    });
    return out.filter(function (x) { return x.date; });
  }

  function cluster(items, name) {
    items.forEach(function (x) { x._bg = bigrams(norm(x.title, name)); x._d = dnum(x.date); });
    items.sort(function (a, b) { return (b._d - a._d) || (KIND_RANK[b.kind] - KIND_RANK[a.kind]); });
    var groups = [];
    items.forEach(function (x) {
      var hit = null;
      for (var i = 0; i < groups.length && !hit; i++) {
        var g = groups[i];
        for (var j = 0; j < g.length; j++) {
          var y = g[j];
          if (x.nid && x.nid === y.nid) { hit = g; x._sim = 1; break; }  /* 事件与其源新闻是同一对象，强制同簇 */
          if (Math.abs(x._d - y._d) > 2) continue;
          if (opposite(x.title, y.title)) continue;
          var sim = dice(x._bg, y._bg);
          if (sim >= 0.5 || (x.category === y.category && MERGE_CATS[x.category] && x.kind !== y.kind)) { hit = g; x._sim = sim; break; }
        }
      }
      if (hit) hit.push(x); else groups.push([x]);
    });
    return groups.map(function (g) {
      g.sort(function (a, b) { return (KIND_RANK[b.kind] - KIND_RANK[a.kind]) || (b._d - a._d); });
      var main = g[0];
      var sent = main.sentiment;
      if (main.kind !== "media") {
        /* 公告本身情绪中性时，取媒体报道的多数情绪 */
        var cnt = { "利好": 0, "利空": 0 };
        g.forEach(function (x) { if (x.kind === "media" && cnt[x.sentiment] != null) cnt[x.sentiment]++; });
        if (sent === "中性" && cnt["利好"] !== cnt["利空"]) sent = cnt["利好"] > cnt["利空"] ? "利好" : "利空";
      }
      var kinds = {};
      g.forEach(function (x) { kinds[x.kind] = 1; });
      var latest = g.reduce(function (m, x) { return x.date > m ? x.date : m; }, main.date);
      var clean = function (x) { var o = {}; for (var k in x) if (k.charAt(0) !== "_") o[k] = x[k]; return o; };
      return { id: main.kind + ":" + main.date + ":" + main.title.slice(0, 16), date: latest, kind: main.kind,
        kinds: Object.keys(kinds), category: main.category, sentiment: sent, main: clean(main),
        related: g.slice(1).map(clean), size: g.length };
    }).sort(function (a, b) { return b.date.localeCompare(a.date) || (KIND_RANK[b.kind] - KIND_RANK[a.kind]); });
  }

  /* P1-2 规则评分：imp 1-5 + 传导链/方向/时间维度；LLM 评分（src.scores[id]）覆盖规则值 */
  var CAT_BASE = { "监管处罚": 4, "风险提示": 4, "财务披露": 3, "回购": 3, "分红送配": 2, "股权变动": 2, "公司经营": 2 };
  var KEY_UP = /超预期|不及预期|低于预期|高于预期|指引|上调|下调|收购|并购|重组|制裁|禁令|反垄断|辞任|辞职|停牌|退市|私有化|分拆|要约|大额|巨额|禁售|出口管制|关税/;
  var BIG_AMT = /(\d{3,}|\d+(\.\d+)?万)\s*亿|罚[^，。]{0,6}\d+(\.\d+)?\s*亿/;
  var BROKER = /评级|目标价|观点|看好|首予|首次覆盖|研报|证券：|银行：|资本：|花旗|高盛|摩根|大摩|小摩|瑞银|美银|中金|Cowen|Seaport|Jefferies|Bernstein/;
  var KEY_STRONG = /暴跌|暴涨|重挫|大涨|大跌|创新高|创新低|史上|重磅|紧急|突发|腰斩|翻倍/;
  var CHAIN = {
    "财务披露": "业绩 → 盈利预期修正 → 估值重定价",
    "回购": "回购 → 流通股减少/管理层信号 → EPS 与估值支撑",
    "分红送配": "分红 → 股东回报率 → 长线资金偏好",
    "监管处罚": "监管 → 合规成本/业务受限 → 盈利与风险溢价",
    "股权变动": "股东增减持 → 内部人信号/供给压力 → 短期资金面",
    "风险提示": "风险事件 → 不确定性上升 → 风险溢价抬升",
    "公司经营": "经营动作 → 收入/成本预期 → 中期基本面"
  };
  /* 例行程序性公告：董事会会议通知、月报表、翌日披露等，封顶 2 分 */
  var ROUTINE = /董事会会议|举行董事会|月报表|翌日披露|证券变动月报|股东周年大会通告|通函|委任代表表格|更换.*代理/;
  var HORIZON = { "财务披露": "中期", "回购": "短期", "分红送配": "长期", "监管处罚": "中期", "股权变动": "短期", "风险提示": "短期", "公司经营": "中期" };
  function ruleScore(c) {
    var txt = [c.main.title, c.main.body].concat(c.related.map(function (r) { return r.title; })).join(" ");
    var imp = CAT_BASE[c.category] || 2, why = [c.category + " 基础 " + imp];
    if (c.size >= 3) { imp++; why.push("多源报道 ×" + c.size); }
    var k = KEY_UP.exec(txt); if (k) { imp++; why.push("关键词「" + k[0] + "」"); }
    var s = KEY_STRONG.exec(txt); if (s) { imp++; why.push("强烈措辞「" + s[0] + "」"); }
    var a = (c.kind !== "media" || /监管处罚|回购|风险提示|股权变动/.test(c.category)) && BIG_AMT.exec(txt);
    if (a) { imp++; why.push("大额「" + a[0] + "」"); }
    if (c.kind === "media" && c.size < 3 && BROKER.test(c.main.title || "") && imp > 3) { imp = 3; why.push("单家券商观点封顶 3"); }
    if (c.kinds.indexOf("official") >= 0 && imp < 3) { imp = 3; why.push("交易所公告保底 3"); }
    if (ROUTINE.test(c.main.title || "")) { imp = Math.min(imp, 2); why.push("例行程序性公告封顶 2"); }
    imp = Math.max(1, Math.min(5, imp));
    var dir = c.sentiment === "利好" ? "正面" : c.sentiment === "利空" ? "负面" : "中性";
    return { imp: imp, direction: dir, horizon: HORIZON[c.category] || "中期", chain: CHAIN[c.category] || CHAIN["公司经营"], why: why.join("；"), src: "rule" };
  }
  function applyScore(c, scores) {
    var r = ruleScore(c), l = scores && scores[c.id];
    c.score = r;
    if (l && l.imp) {
      c.score = { imp: Math.max(1, Math.min(5, +l.imp)), direction: l.direction || r.direction, horizon: l.horizon || r.horizon,
        chain: l.chain || r.chain, why: l.why || r.why, src: l.src || "llm", rule_imp: r.imp, scored_at: l.scored_at || "" };
    }
    c.imp = c.score.imp;
    return c;
  }

  function upcoming(code, src, today) {
    var t0 = dnum(today), out = [];
    var ev = src.events || {};
    (ev.upcoming_earnings || []).forEach(function (e) {
      if (e.code !== code) return;
      out.push({ type: "earnings", date: day(e.date), title: "财报披露" + (e.period ? "（" + e.period + "）" : ""), note: e.note || "" });
    });
    var th = src.theses && src.theses.items ? src.theses.items[code] : null;
    if (th && th.review_date) out.push({ type: "thesis", date: day(th.review_date), title: "复查我的判断", note: th.summary || "" });
    return out.map(function (u) { u.days = isNaN(t0) ? null : dnum(u.date) - t0; return u; })
      .filter(function (u) { return u.days == null || (u.days >= -3 && u.days <= 90); })
      .sort(function (a, b) { return a.date.localeCompare(b.date); });
  }

  function build(code, src) {
    src = src || {};
    var today = src.today || new Date().toISOString().slice(0, 10);
    var raw = collect(code, src);
    var name = raw.length ? raw[0].name : "";
    var cl = cluster(raw, name).map(function (c) { return applyScore(c, src.scores); });
    var byKind = {}, byCat = {}, bySenti = {}, byImp = {};
    raw.forEach(function (x) { byKind[x.kind] = (byKind[x.kind] || 0) + 1; });
    cl.forEach(function (c) {
      byCat[c.category] = (byCat[c.category] || 0) + 1;
      bySenti[c.sentiment] = (bySenti[c.sentiment] || 0) + 1;
      byImp[c.imp] = (byImp[c.imp] || 0) + 1;
    });
    return { code: code, today: today, upcoming: upcoming(code, src, today), items: cl,
      stats: { raw: raw.length, clusters: cl.length, merged: raw.length - cl.length, byKind: byKind, byCat: byCat, bySenti: bySenti, byImp: byImp,
        high: cl.filter(function (c) { return c.imp >= 4; }).length } };
  }

  function filter(tl, f) {
    f = f || {};
    return tl.items.filter(function (c) {
      if (f.cat && f.cat !== "全部" && c.category !== f.cat) return false;
      if (f.senti && f.senti !== "全部" && c.sentiment !== f.senti) return false;
      if (f.official && c.kinds.indexOf("official") < 0 && c.kinds.indexOf("event") < 0) return false;
      if (f.minImp && (c.imp || 0) < f.minImp) return false;
      return true;
    }).sort(function (a, b) {
      if (f.sort === "imp") return ((b.imp || 0) - (a.imp || 0)) || b.date.localeCompare(a.date);
      return 0;
    });
  }

  return { build: build, filter: filter, KIND_LABEL: KIND_LABEL, inferCat: inferCat, ruleScore: ruleScore };
});
