/*
 * settings-ui.js — 面板「设置」中心前端（harness 独立版）
 *   入口：侧栏品牌栏齿轮 → selected="settings"，state.tab = set-<分组>
 *   分组：模型 / Prompt / 问答 / 组合红线 / 大师 / 推送与自动任务 / 显示
 *   每组独立保存（POST /api/settings {section, value}），改动项显示「已改」，未保存时 SET.dirty()=true（阻止定时重绘冲掉草稿）
 *   与 8899 的差异：无发现选股与 WorkBuddy 同步分组；问答不支持图片/文件附件（后端硬约束）。
 */
(function () {
  "use strict";
  function W() { return window.WB; }
  function S() {
    var s = W().state;
    s.set = s.set || { meta: null, loading: false, err: null, dirty: {}, test: {}, open: {} };
    return s.set;
  }
  function esc(v) { return W().esc(v == null ? "" : v); }
  function get(o, p) { return p.split(".").reduce(function (a, k) { return a == null ? a : a[k]; }, o); }
  function put(o, p, v) { var ks = p.split("."), i, c = o; for (i = 0; i < ks.length - 1; i++) { c[ks[i]] = c[ks[i]] || {}; c = c[ks[i]]; } c[ks[ks.length - 1]] = v; }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

  var GROUPS = [
    ["model", "模型", "全局模型与各场景覆盖"],
    ["prompts", "Prompt", "问答、逻辑卡、事件评分、大师"],
    ["ask", "问答", "上下文与深度模式"],
    ["rules", "组合红线", "集中度与止损纪律"],
    ["decide", "决策口径", "长期四维权重与结论阈值"],
    ["personas", "大师", "参与分析的思维框架"],
    ["notify", "推送与自动任务", "通道、静默与后台任务"],
    ["display", "显示", "刷新频率与隐私"],
  ];

  var SET = {};
  SET.load = function (cb) {
    var st = S();
    if (st.loading) return;
    st.loading = true;
    W().fetchOpt("/api/settings").then(function (d) {
      st.loading = false;
      if (!d || !d.settings) { st.err = "设置加载失败"; }
      else { st.meta = d; st.err = null; W().state.settings = d.settings; }
      if (cb) cb();
      if (isSel() && !SET.dirty()) W().renderMain();
    });
  };
  SET.dirty = function () { var d = S().dirty; return Object.keys(d).some(function (k) { return d[k]; }); };
  function isSel() { return W().state.selected === "settings"; }
  SET.open = function (grp) { W().go("settings", "set-" + (grp || "model")); if (!S().meta) SET.load(); };

  /* ---------- 字段渲染 ---------- */
  function cur(sec) { var m = S().meta; return sec === "rules" ? m.rules : sec === "notify" ? m.notify : m.settings[sec]; }
  function def(sec) { var m = S().meta; return sec === "rules" || sec === "notify" ? null : m.defaults[sec]; }
  function changed(sec, k) { var d = def(sec); return d ? !same(get(cur(sec), k), get(d, k)) : false; }
  function mark(sec, k) { return changed(sec, k) ? '<span class="st-mod" title="默认：' + esc(JSON.stringify(get(def(sec), k))) + '">已改</span>' : ""; }

  function num(sec, k, label, o) {
    o = o || {};
    var v = get(cur(sec), k);
    return '<label class="st-f' + (o.wide ? " wide" : "") + '"><span class="l">' + esc(label) + mark(sec, k) + '</span>' +
      '<span class="in"><input type="number" data-k="' + k + '" data-t="num" value="' + (v == null ? "" : esc(v)) + '"' +
      (o.step ? ' step="' + o.step + '"' : "") + (o.min != null ? ' min="' + o.min + '"' : "") + (o.max != null ? ' max="' + o.max + '"' : "") +
      (o.ph ? ' placeholder="' + esc(o.ph) + '"' : "") + '>' + (o.unit ? '<em>' + esc(o.unit) + '</em>' : "") + '</span>' +
      (o.hint ? '<span class="h">' + esc(o.hint) + '</span>' : "") + '</label>';
  }
  function txt(sec, k, label, o) {
    o = o || {};
    var v = get(cur(sec), k);
    return '<label class="st-f' + (o.wide ? " wide" : "") + '"><span class="l">' + esc(label) + mark(sec, k) + '</span>' +
      '<span class="in"><input type="text" data-k="' + k + '" data-t="str" value="' + esc(v == null ? "" : v) + '"' + (o.ph ? ' placeholder="' + esc(o.ph) + '"' : "") + ' spellcheck="false"></span>' +
      (o.hint ? '<span class="h">' + esc(o.hint) + '</span>' : "") + '</label>';
  }
  function tog(sec, k, label, hint, v) {
    if (v === undefined) v = get(cur(sec), k);
    return '<label class="st-tg"><input type="checkbox" data-k="' + k + '" data-t="bool"' + (v ? " checked" : "") + '><i></i>' +
      '<span class="t"><b>' + esc(label) + mark(sec, k) + '</b>' + (hint ? '<small>' + esc(hint) + '</small>' : "") + '</span></label>';
  }
  function actions(sec, o) {
    o = o || {};
    return '<div class="st-act">' + (o.extra || "") +
      (o.noReset ? "" : '<button class="ds-ign" data-st-reset="' + sec + '">恢复默认</button>') +
      '<button class="ds-add big" data-st-save="' + sec + '" disabled>保存</button></div>';
  }
  function card(title, desc, body) {
    return '<section class="st-card"><div class="st-ch"><h3>' + esc(title) + '</h3>' + (desc ? '<p>' + desc + '</p>' : "") + '</div>' + body + '</section>';
  }

  /* ---------- 各分组 ---------- */
  function gModel() {
    var m = S().meta, sc = m.scenes, sec = m.secrets, t = S().test;
    var rows = Object.keys(sc).map(function (k) {
      var r = t[k];
      var res = !r ? "" : r.busy ? '<span class="st-muted">测试中…</span>' : r.ok ? '<span class="st-ok">通 · ' + esc(r.model) + " · " + r.ms + "ms</span>" : '<span class="st-bad" title="' + esc(r.error) + '">失败 · ' + esc(String(r.error || "").slice(0, 40)) + "</span>";
      return '<tr><td class="nm">' + esc(sc[k]) + '</td>' +
        '<td><input type="text" data-k="scenes.' + k + '.model" data-t="str" value="' + esc(get(m.settings.model, "scenes." + k + ".model") || "") + '" placeholder="跟随全局" spellcheck="false"></td>' +
        '<td><input type="number" step="0.1" min="0" max="2" data-k="scenes.' + k + '.temperature" data-t="numn" value="' + esc(get(m.settings.model, "scenes." + k + ".temperature") == null ? "" : get(m.settings.model, "scenes." + k + ".temperature")) + '" placeholder="跟随"></td>' +
        '<td class="r"><button class="ds-ign" data-st-test="' + k + '">测试</button></td><td class="res">' + res + '</td></tr>';
    }).join("");
    return card("全局模型", "面板内所有 LLM 调用默认使用这里的配置（OpenAI 兼容 Chat Completions 接口），定时任务与页面实时调用共用。",
      '<div class="st-grid">' +
      txt("model", "model", "模型名", { ph: "deepseek-v4-flash", hint: "如 deepseek-v4-flash / deepseek-chat / deepseek-reasoner" }) +
      num("model", "temperature", "Temperature", { step: 0.1, min: 0, max: 2, hint: "研究类建议 0.2–0.4，越低越稳定" }) +
      num("model", "timeout_s", "超时", { unit: "秒", min: 10, max: 600 }) +
      txt("model", "url", "接口地址", { wide: true, hint: "默认 DeepSeek 官方；可换成任意 OpenAI 兼容端点" }) +
      '</div>') +
      card("场景覆盖", "某个场景需要更强或更省的模型时单独指定；留空即跟随全局。",
        '<table class="st-tb"><thead><tr><th>场景</th><th>模型</th><th>Temperature</th><th></th><th></th></tr></thead><tbody>' + rows + '</tbody></table>') +
      actions("model") +
      card("API Key", "Key 只写入本机 harness/.env，页面不显示原文，也不会进入 settings.json。",
        '<div class="st-key"><span class="st-kv">' + (sec.deepseek && sec.deepseek.configured ? "已配置" + (sec.deepseek.masked ? " <code>" + esc(sec.deepseek.masked) + "</code>" : "") : '<span class="st-bad">未配置</span>') + '</span>' +
        '<input type="password" id="stKey" placeholder="粘贴新的 DeepSeek API Key 以替换" autocomplete="off">' +
        '<button class="ds-add" id="stKeySave">更新 Key</button></div>');
  }

  function gPrompts() {
    var m = S().meta, P = m.settings.prompts, F = m.fixed;
    var defs = [
      ["ask_system", "投资Buddy · 系统提示", "右侧问答。可用占位符 <code>{today}</code>（北京日期）、<code>{title}</code>（当前讨论对象，必须保留）；面板资料自动附在末尾。"],
      ["draft_system", "逻辑卡 AI 草稿 · 写作要求", "决定买入理由、证伪条件、跟踪指标的写法。JSON 输出结构为固定段，保证能被解析。"],
      ["events_system", "事件评分 · 角色", "给新闻 / 公告做 1–5 分重要性分级时的角色设定。"],
      ["events_rubric", "事件评分 · 口径补充", "打分时的特别说明，决定哪些事件会被推到 4–5 分并推送。"],
      ["expert_suffix", "大师分析 · 附加约束", "接在大师框架之后，面板内大师分析生成时使用。"],
    ];
    var body = defs.map(function (d) {
      var k = d[0], ch = changed("prompts", k);
      var fx = F[k] ? '<div class="st-fixed"><span>固定段 · 不可编辑</span>' + esc(F[k]) + '</div>' : "";
      return '<div class="st-pr' + (S().open[k] ? " open" : "") + '" data-pr="' + k + '">' +
        '<button class="st-prh" data-st-pr="' + k + '"><b>' + esc(d[1]) + '</b>' + (ch ? '<span class="st-mod">已改</span>' : "") +
        '<span class="st-muted">' + esc(String(P[k]).length) + ' 字</span><span class="car">' + (S().open[k] ? "收起" : "编辑") + '</span></button>' +
        '<div class="st-prb"><p class="st-note">' + d[2] + '</p>' +
        '<textarea data-k="' + k + '" data-t="str" rows="' + Math.min(14, String(P[k]).split("\n").length + 2) + '" spellcheck="false">' + esc(P[k]) + '</textarea>' + fx +
        (ch ? '<div class="st-prr"><button class="ds-link" data-st-reset="prompts.' + k + '">恢复此项默认</button></div>' : "") + '</div></div>';
    }).join("");
    return card("Prompt 模板", "只开放「写作口径」部分；JSON 结构与防提示词注入条款是固定段，改坏也不会让解析失败或被资料里的指令劫持。", body) + actions("prompts");
  }

  function gAsk() {
    return card("问答上下文", "提问时会把当前个股在面板里的资料（持仓、计划、笔记、逻辑卡、信号、估值、财报、事件、大师观点…）按优先级装进上下文，随问题发送给你配置的模型接口；超过上限时先截断低优先级资料。",
      '<div class="st-grid">' +
      num("ask", "ctx_chars", "上下文上限", { unit: "字", min: 8000, max: 200000, step: 1000, hint: "6 万字约 4 万 token，多轮追问会命中前缀缓存" }) +
      num("ask", "hist_turns", "保留对话轮数", { unit: "轮", min: 2, max: 40 }) + "</div>" +
      '<h4 class="st-sub">深度模式 · ReAct（拆解 → 调工具查证 → 再推理）</h4><div class="st-tgs">' +
      '<label class="st-tg"><input type="checkbox" data-k="mode" data-t="mode"' + (get(cur("ask"), "mode") !== "direct" ? " checked" : "") + '><i></i><span class="t"><b>默认使用深度模式' + mark("ask", "mode") + '</b><small>模型可调用面板内的行情、财报、事件等工具逐步核实；问答栏可按次切换</small></span></label></div>' +
      '<div class="st-grid">' + num("ask", "max_steps", "最多推理步数", { unit: "步", min: 2, max: 10, hint: "每步可并行调用多个工具" }) +
      num("ask", "max_tokens", "单轮输出额度", { unit: "tokens", min: 2000, max: 32000, hint: "推理型模型的思考与正文共用此额度；回答常被截断或报「未返回可用回答」时调大" }) + "</div>") +
      actions("ask") +
      card("联网搜索", "开启后模型可按需调用网页搜索/个股资讯/网页全文；搜索词只允许公开实体（公司简称/行业/宏观词），服务端会拦截携带持仓数量、成本、仓位等私有数据的搜索词。",
        '<div data-sub="search"><div class="st-tgs">' + tog("search", "enabled", "启用联网搜索", "默认开启；关闭后问答仅用本机面板资料") +
        tog("search", "stock_news", "个股资讯（westock）", "腾讯自选股源，免费结构化，比网页搜索更新更准") +
        tog("search", "read_url", "网页全文读取", "仅放行公网地址（防 SSRF）") + '</div>' +
        '<div class="st-grid">' + num("search", "max_calls", "每轮联网上限", { unit: "次", min: 1, max: 8 }) +
        num("search", "results", "每次结果数", { unit: "条", min: 3, max: 10 }) + '</div>' +
        '<div class="st-tgs">' + tog("search", "providers.sogou", "搜狗", "免费网页源") +
        tog("search", "providers.so360", "360 搜索", "免费网页源") +
        tog("search", "providers.baidu", "百度", "免费网页源") +
        tog("search", "providers.bocha", "博查", "付费 API，需 BOCHA_API_KEY") +
        tog("search", "providers.tavily", "Tavily", "付费 API，需 TAVILY_API_KEY（免费 1000 次/月）") + '</div></div>' + actions("search")) +
      card("能力边界", "出于私有持仓数据外发风险与当前实现范围，以下能力在本版本中固定关闭：",
        '<ul class="st-note-list"><li>图片 / 文件附件识别</li><li>答完自检（reflect）</li></ul>');
  }

  function gDecide() {
    var m = S().meta, d = m.settings.decide, dims = m.dims;
    var sum = dims.reduce(function (a, x) { return a + Number(d.weights[x[0]] || 0); }, 0);
    var wRows = dims.map(function (x) {
      var v = Number(d.weights[x[0]] || 0), pct = sum > 0 ? v / sum * 100 : 0;
      return '<div class="st-w"><span class="n">' + esc(x[1]) + mark("decide", "weights." + x[0]) + '</span>' +
        '<input type="range" min="0" max="0.5" step="0.05" data-k="weights.' + x[0] + '" data-t="num" value="' + v + '">' +
        '<span class="v" data-wv="' + x[0] + '">' + pct.toFixed(0) + '%</span></div>';
    }).join("");
    return card("长期四维权重", "综合分 = Σ(维度分 × 权重) / Σ有数据维度的权重，维度分 0–100（60 及格）；只看公司本身（成本、仓位、预警只进纪律闸门）。右侧为归一化后的实际占比。", '<div class="st-ws">' + wRows + '</div>') +
      card("结论阈值", "综合分落在哪一档决定「可加仓 / 持有 / 减仓审视 / 清仓审视」（持仓）与「可建仓 / 等待买点 / 继续观察 / 暂缓」（关注）。",
        '<div class="st-ladder">' +
        '<div><b class="lv bullish">偏多</b>' + num("decide", "hi", "综合分 ≥", { step: 1, unit: "分" }) + '</div>' +
        '<div><b class="lv normal">持有</b>' + num("decide", "mid", "综合分 ≥", { step: 1, unit: "分" }) + '</div>' +
        '<div><b class="lv neutral">审视</b>' + num("decide", "lo", "综合分 ≥", { step: 1, unit: "分" }) + '</div></div>' +
        '<div class="st-grid">' +
        num("decide", "add_upside", "可加仓需目标价空间", { unit: "%", hint: "偏多档持仓，空间达到此值才提示「可加仓」" }) +
        num("decide", "build_upside", "可建仓需目标价空间", { unit: "%" }) +
        num("decide", "margin", "安全边际线", { unit: "%", hint: "目标价空间达到此值，估值项得满分 2" }) +
        num("decide", "roe", "ROE 达标线", { unit: "%" }) +
        num("decide", "debt", "负债率上限", { unit: "%" }) +
        num("decide", "pos_max", "单只仓位上限", { unit: "%", hint: "纪律闸门：全组合人民币市值占比超过则要求减仓（纪律规则里的单股上限优先）" }) +
        '</div>') + actions("decide");
  }

  function gRules() {
    var sec = "rules";
    return card("集中度红线", "组合总览「诊断」与交易前纪律检查共用。保存后立即生效。", '<div class="st-grid">' +
      num(sec, "single_max", "单只上限", { unit: "%" }) + num(sec, "single_warn", "单只预警", { unit: "%" }) +
      num(sec, "top3_max", "前三合计上限", { unit: "%" }) + num(sec, "sector_max", "单一赛道上限", { unit: "%" }) +
      num(sec, "market_max", "单一市场上限", { unit: "%" }) + num(sec, "fx_shock_pct", "汇率冲击提示", { unit: "%" }) + '</div>') +
      card("亏损纪律", "", '<div class="st-grid">' +
        num(sec, "stop_review_pct", "个股止损审视线", { unit: "%", hint: "浮亏达到即触发「止损审视」，优先于一切观点" }) +
        num(sec, "deep_loss_pct", "深度亏损线", { unit: "%" }) + num(sec, "deep_loss_count_max", "深度亏损只数上限", { unit: "只" }) +
        num(sec, "portfolio_loss_max", "组合浮亏告警", { unit: "%" }) + '</div>') +
      actions(sec, { noReset: true });
  }

  function gPersonas() {
    var m = S().meta, on = m.settings.personas || {};
    var list = function (scope) {
      return m.personas.filter(function (p) { return p.scope === scope; }).map(function (p) {
        return tog("personas", p.id, p.name + " · " + (p.title || ""), p.lens + "  〔" + p.skill + "〕", on[p.id] !== false);
      }).join("");
    };
    return card("个股大师", "参与个股「大师」Tab 的分析生成。停用后不再生成新观点。", '<div class="st-tgs">' + list("stock") + '</div>') +
      card("组合层大师", "只在组合层面发表观点，不参与个股分析。", '<div class="st-tgs">' + list("portfolio") + '</div>') +
      actions("personas");
  }

  function gNotify() {
    var sec = S().meta.secrets;
    var a = function (k, l, h) { return tog("auto", k, l, h); };
    return card("推送通道", "预警、组合红线、高分事件都通过这里送达。", '<div data-sub="notify"><div class="st-tgs">' +
      tog("notify", "enabled", "推送总开关", "关闭后只在面板内显示") +
      tog("notify", "channels.wecom.enabled", "企业微信机器人", sec.wecom && sec.wecom.configured ? "Webhook 已配置" : "未配置 Webhook") +
      tog("notify", "quiet.enabled", "静默时段", "时段内只记录不推送") + '</div>' +
      '<div class="st-grid">' + num("notify", "cooldown_min", "同一预警冷却", { unit: "分钟", min: 5, max: 1440 }) +
      txt("notify", "quiet.start", "静默开始", { ph: "00:30" }) + txt("notify", "quiet.end", "静默结束", { ph: "07:30" }) + '</div>' +
      '<div class="st-key"><input type="password" id="stWecom" placeholder="粘贴新的企业微信 Webhook 以替换（https://qyapi.weixin.qq.com/…key=…）" autocomplete="off"><button class="ds-add" id="stWecomSave">更新 Webhook</button><button class="ds-ign" id="stNotifyTest">发送测试</button></div>' +
      '</div>') + actions("notify", { noReset: true }) +
      card("后台任务", "面板服务进程内的定时任务，切换后下一个周期即生效，无需重启。", '<div data-sub="auto"><div class="st-tgs">' +
        a("alert_watch", "服务端盯盘", "工作日每小时检查价格预警与止损线") +
        a("panel_build", "面板数据重建", "工作日 08:40 / 16:20 / 21:15 增量，周五 16:40 全量（含一页纸）") +
        a("experts_refresh", "大师分析周更", "周六 09:00 为持仓与关注标的刷新大师观点") +
        a("review_weekly", "周度复盘", "周六 09:30 生成上周组合复盘并推送") +
        a("consensus_daily", "机构共识快照", "每日 08:05 记录目标价/评级，供 4 周变化对比") +
        a("preopen_brief", "盘前简报", "工作日 21:30 生成美股盘前简报") + '</div></div>') + actions("auto");
  }

  function gDisplay() {
    var priv = !!W().state.priv;
    return card("刷新频率", "", '<div class="st-grid">' +
      num("display", "poll_sec", "行情轮询", { unit: "秒", min: 3, max: 120, hint: "交易时段报价刷新间隔" }) +
      num("display", "reload_min", "数据重载", { unit: "分钟", min: 1, max: 60, hint: "重新读取面板分析数据" }) + '</div>') +
      actions("display") +
      card("隐私", "与组合总览上的眼睛按钮是同一个开关，本机记住上次选择。",
        '<label class="st-tg"><input type="checkbox" id="stPriv"' + (priv ? " checked" : "") + '><i></i><span class="t"><b>隐藏金额</b><small>市值、盈亏金额显示为 ****，比例与涨跌幅保留</small></span></label>');
  }

  var RENDER = { model: gModel, prompts: gPrompts, ask: gAsk, rules: gRules, decide: gDecide, personas: gPersonas, notify: gNotify, display: gDisplay };

  SET.renderMain = function () {
    var st = S();
    if (!st.meta) { if (!st.loading) SET.load(); return '<div class="empty">' + (st.err ? esc(st.err) : "加载设置…") + '</div>'; }
    var grp = String(W().state.tab || "").replace(/^set-/, "");
    if (!RENDER[grp]) grp = "model";
    var nav = GROUPS.map(function (g) {
      var dot = st.dirty[g[0]] || (g[0] === "notify" && st.dirty.auto) ? '<i class="st-dot ink"></i>' : "";
      return '<button class="st-nav' + (g[0] === grp ? " on" : "") + '" data-st-go="' + g[0] + '"><b>' + esc(g[1]) + dot + '</b><small>' + esc(g[2]) + '</small></button>';
    }).join("");
    return '<div class="stock-head"><div><h1>设置</h1><div class="meta-bar">模型、Prompt、组合红线与自动任务 · 保存即生效 · 数据存于本机 harness/data/config/</div></div></div>' +
      '<div class="st-wrap"><nav class="st-side">' + nav + '</nav><div class="st-body" data-grp="' + grp + '">' + RENDER[grp]() + '</div></div>';
  };

  /* ---------- 交互 ---------- */
  function secOf(el) {
    var sub = el.closest("[data-sub]");
    if (sub) return sub.getAttribute("data-sub");
    return el.closest(".st-body").getAttribute("data-grp");
  }
  function collect(sec) {
    var body = document.querySelector(".st-body"), out = {};
    var scope = body.querySelector('[data-sub="' + sec + '"]');
    var nodes = (scope || body).querySelectorAll("[data-k]");
    Array.prototype.forEach.call(nodes, function (el) {
      if (!scope && el.closest("[data-sub]")) return;
      var t = el.getAttribute("data-t"), v;
      if (t === "bool") v = el.checked;
      else if (t === "mode") v = el.checked ? "react" : "direct";
      else if (t === "num" || t === "numn") v = el.value === "" ? null : Number(el.value);
      else v = el.value;
      if ((t === "num" || t === "numn") && v == null) return;
      put(out, el.getAttribute("data-k"), v);
    });
    return out;
  }
  function saveBtn(sec) {
    var b = document.querySelector('[data-st-save="' + sec + '"]');
    if (b) { b.disabled = !S().dirty[sec]; b.textContent = S().dirty[sec] ? "保存" : "已保存"; }
  }
  function toServer(sec, v) {
    if (sec === "notify") {
      return { section: "notify", value: { enabled: v.enabled, wecom: get(v, "channels.wecom.enabled"), cooldown_min: v.cooldown_min, quiet: v.quiet } };
    }
    return { section: sec, value: v };
  }
  function save(sec) {
    var v = collect(sec);
    W().apiPost("/api/settings", toServer(sec, v)).then(function (r) {
      if (!r || !r.ok) { W().toast((r && r.error) || "保存失败"); return; }
      S().dirty[sec] = false;
      W().toast("已保存，已生效");
      afterSave(sec);
    });
  }
  function afterSave(sec) {
    var w = W();
    SET.load(function () {
      if (sec === "personas" || sec === "rules") w.state.diag = null;
      if (sec === "display" && window.WB_RESCHED) window.WB_RESCHED();
    });
  }

  SET.bind = function () {
    var main = document.getElementById("main"), st = S();
    if (!main.querySelector(".st-wrap")) return;
    main.querySelectorAll("[data-st-go]").forEach(function (b) {
      b.addEventListener("click", function () {
        if (SET.dirty() && !confirm("当前分组有未保存的修改，确定离开？")) return;
        st.dirty = {}; W().go("settings", "set-" + b.getAttribute("data-st-go"));
      });
    });
    main.querySelectorAll(".st-body [data-k]").forEach(function (el) {
      var ev = el.type === "checkbox" ? "change" : "input";
      el.addEventListener(ev, function () {
        var sec = secOf(el); st.dirty[sec] = true; saveBtn(sec);
        var wv = el.getAttribute("data-k") || "";
        if (sec === "decide" && wv.indexOf("weights.") === 0) {
          var meta = S().meta, d = collect("decide");
          var sum = 0; (meta.dims || []).forEach(function (x) { sum += Number((d.weights || {})[x[0]] != null ? d.weights[x[0]] : (meta.settings.decide.weights[x[0]] || 0)); });
          var span = document.querySelector('[data-wv="' + wv.slice(8) + '"]');
          if (span && sum > 0) span.textContent = (Number(el.value) / sum * 100).toFixed(0) + "%";
        }
      });
    });
    main.querySelectorAll("[data-st-save]").forEach(function (b) { saveBtn(b.getAttribute("data-st-save")); b.addEventListener("click", function () { save(b.getAttribute("data-st-save")); }); });
    main.querySelectorAll("[data-st-reset]").forEach(function (b) {
      b.addEventListener("click", function () {
        var sec = b.getAttribute("data-st-reset");
        if (!confirm(sec.indexOf("prompts.") === 0 ? "恢复这一段 Prompt 为默认？" : "恢复本分组全部为默认值？")) return;
        W().apiPost("/api/settings/reset", { section: sec }).then(function (r) {
          if (!r || !r.ok) { W().toast("恢复失败"); return; }
          st.dirty = {}; W().toast("已恢复默认"); afterSave(sec.split(".")[0]);
        });
      });
    });
    main.querySelectorAll("[data-st-pr]").forEach(function (b) {
      b.addEventListener("click", function () {
        var k = b.getAttribute("data-st-pr"); st.open[k] = !st.open[k];
        b.parentNode.classList.toggle("open", st.open[k]);
        b.querySelector(".car").textContent = st.open[k] ? "收起" : "编辑";
      });
    });
    main.querySelectorAll("[data-st-test]").forEach(function (b) {
      b.addEventListener("click", function () {
        var k = b.getAttribute("data-st-test");
        if (st.dirty.model) { W().toast("先保存模型设置，再测试"); return; }
        st.test[k] = { busy: true }; W().renderMain();
        W().apiPost("/api/settings/test-model", { scene: k }).then(function (r) { st.test[k] = r || { ok: false, error: "无响应" }; if (!SET.dirty()) W().renderMain(); });
      });
    });
    var q = function (id) { return document.getElementById(id); };
    if (q("stKeySave")) q("stKeySave").addEventListener("click", function () {
      var v = q("stKey").value.trim(); if (!v) { W().toast("请先粘贴 Key"); return; }
      W().apiPost("/api/settings", { section: "secrets", deepseek_key: v }).then(function (r) { W().toast(r && r.ok ? "Key 已更新" : (r && r.error) || "更新失败"); if (r && r.ok) SET.load(); });
    });
    if (q("stWecomSave")) q("stWecomSave").addEventListener("click", function () {
      var v = q("stWecom").value.trim(); if (!v) { W().toast("请先粘贴 Webhook"); return; }
      W().apiPost("/api/settings", { section: "secrets", wecom_url: v }).then(function (r) { W().toast(r && r.ok ? "Webhook 已更新" : (r && r.error) || "更新失败"); if (r && r.ok) SET.load(); });
    });
    if (q("stNotifyTest")) q("stNotifyTest").addEventListener("click", function () {
      W().apiPost("/api/notify/test", {}).then(function (r) { W().toast(r && r.ok !== false ? "测试推送已发送" : "推送失败：" + ((r && r.error) || "")); });
    });
    if (q("stPriv")) q("stPriv").addEventListener("change", function () {
      W().state.priv = this.checked; try { localStorage.setItem("wb.privacy", this.checked ? "1" : "0"); } catch (e) {}
      W().toast(this.checked ? "金额已隐藏" : "金额已显示");
    });
  };

  window.SET = SET;
})();
