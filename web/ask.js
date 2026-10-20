/*
 * ask.js — 「投资Buddy」右侧助手栏（前端）
 * 依赖 window.WB 桥（state / findStock / esc）。上下文跟随当前选中的个股；组合/决策/主题页 → 组合口径。
 * 会话按标的存 localStorage（wb.ask.v1.<code>，每只最多 30 条），右上角「+」新对话，「⇥」收起。
 */
(function () {
  "use strict";
  var LS = "wb.ask.v1.", LS_UI = "wb.ask.ui";
  var ICON_SPARK = '<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none"><path d="M12 2.5c.5 4.6 2.9 7 7.5 7.5v.1c-4.6.5-7 2.9-7.5 7.5h-.1c-.5-4.6-2.9-7-7.5-7.5V10c4.6-.5 7-2.9 7.5-7.5z" fill="currentColor"/><path d="M19 15.5c.2 1.9 1.2 2.9 3 3v.1c-1.8.2-2.8 1.2-3 3h-.1c-.2-1.8-1.2-2.8-3-3v-.1c1.8-.1 2.8-1.1 3-3z" fill="currentColor" opacity=".55"/></svg>';
  function spark(s) { return ICON_SPARK.replace(/\{s\}/g, s); }
  var I_PLUS = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
  var I_SIDE = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><rect x="2.25" y="2.75" width="11.5" height="10.5" rx="2" stroke="currentColor" stroke-width="1.3"/><path d="M10 3v10" stroke="currentColor" stroke-width="1.3"/></svg>';
  var I_UP = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 11.5V2.8M3 6.5 7 2.5l4 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var I_STOP = '<svg width="12" height="12" viewBox="0 0 12 12"><rect x="2" y="2" width="8" height="8" rx="1.5" fill="currentColor"/></svg>';
  var I_COPY = '<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><rect x="4.5" y="4.5" width="7.5" height="7.5" rx="1.6" stroke="currentColor" stroke-width="1.2"/><path d="M9.5 2.5H3.6c-.6 0-1.1.5-1.1 1.1v5.9" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>';
  var I_GLOBE = '<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5.3" stroke="currentColor" stroke-width="1.2"/><path d="M1.9 7h10.2M7 1.7c1.5 1.5 2.2 3.3 2.2 5.3S8.5 10.8 7 12.3C5.5 10.8 4.8 9 4.8 7S5.5 3.2 7 1.7z" stroke="currentColor" stroke-width="1.1"/></svg>';
  var I_CHEV = '<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M3.5 2l3 3-3 3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var PROV = { sogou: "搜狗", so360: "360", baidu: "百度", bocha: "博查", tavily: "Tavily", westock: "自选股资讯" };
  var TOOLN = { web_search: "搜索", stock_news: "个股资讯", read_url: "阅读网页",
    get_quote: "行情", get_kline: "K 线", get_financials: "财报", get_technical: "技术指标", list_reports: "研报列表", read_report: "研报全文", panel_section: "面板资料", lookup_stock: "查代码", calc: "计算" };
  TOOLN.vision = "图片识别"; TOOLN.file = "读取文件";
  var WEBT = { web_search: 1, stock_news: 1, read_url: 1 };
  var I_CLIP = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M13.2 7.6 8 12.8a3.4 3.4 0 0 1-4.8-4.8l5.6-5.6a2.2 2.2 0 0 1 3.1 3.1L6.3 11a1 1 0 0 1-1.5-1.4l5.1-5.1" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var I_DOC = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4 1.8h5.2L12.5 5v8.2c0 .6-.5 1-1 1H4c-.6 0-1-.4-1-1V2.8c0-.6.4-1 1-1z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M9 1.9V5h3.4M5.3 8.2h5M5.3 10.6h3.6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>';
  var I_X = '<svg width="8" height="8" viewBox="0 0 10 10" fill="none"><path d="M2.5 2.5l5 5M7.5 2.5l-5 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
  var MAX_ATT = 6, MAX_MB = 10, IMG_EDGE = 1600, THUMB_EDGE = 160;
  var TEXT_EXT = /\.(txt|md|markdown|csv|tsv|json|log|html?|xml|ya?ml)$/i, BIN_EXT = /\.(pdf|docx?|rtf|xlsx|xlsm)$/i;
  var I_STEPS = '<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><circle cx="3" cy="3.2" r="1.6" stroke="currentColor" stroke-width="1.2"/><circle cx="3" cy="10.8" r="1.6" stroke="currentColor" stroke-width="1.2"/><path d="M3 4.8v4.4M6.5 3.2h5.5M6.5 10.8h5.5M6.5 7h4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>';
  var I_RETRY = '<svg width="13" height="13" viewBox="0 0 14 14" fill="none"><path d="M11.8 7A4.8 4.8 0 1 1 10.4 3.6M11.8 2v2.8H9" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var A = { code: null, msgs: [], busy: false, ctrl: null, sug: {}, model: null, ui: {}, ctx: {}, react: null, pend: [], vision: null };
  function W() { return window.WB; }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function $(id) { return document.getElementById(id); }

  function curCode() {
    var sel = W() && W().state.selected;
    return sel && W().findStock(sel) ? sel : "portfolio";
  }
  function load(code) { try { return JSON.parse(localStorage.getItem(LS + code) || "[]"); } catch (e) { return []; } }
  /* 本地只存缩略图、文件名与识别文本；原图 / 原文件不落 localStorage */
  function slim(m) { if (!m._data) return m; var o = {}; Object.keys(m).forEach(function (k) { if (k !== "_data") o[k] = m[k]; }); return o; }
  function save() { try { localStorage.setItem(LS + A.code, JSON.stringify(A.msgs.filter(function (m) { return !m.pending; }).slice(-30).map(slim))); } catch (e) {} }

  /* ---- 轻量 Markdown（先转义再标记，安全） ---- */
  function inl(s) {
    return esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[([^\]]{1,80})\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/〔([^〕]{1,40})〕/g, '<span class="src">$1</span>');
  }
  function md(src) {
    var L = String(src || "").split(/\r?\n/), out = [], i = 0;
    var cells = function (l) { return l.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(function (c) { return c.trim(); }); };
    while (i < L.length) {
      var l = L[i];
      if (!l.trim()) { i++; continue; }
      var h = l.match(/^(#{1,4})\s+(.*)$/);
      if (h) { out.push("<h3>" + inl(h[2]) + "</h3>"); i++; continue; }
      if (/^\s*\|/.test(l) && i + 1 < L.length && /^\s*\|?\s*:?-{2,}/.test(L[i + 1])) {
        var rows = []; while (i < L.length && /^\s*\|/.test(L[i])) rows.push(L[i++]);
        var hd = cells(rows[0]), bd = rows.slice(2);
        out.push("<table><thead><tr>" + hd.map(function (c) { return "<th>" + inl(c) + "</th>"; }).join("") + "</tr></thead><tbody>" +
          bd.map(function (r) { return "<tr>" + cells(r).map(function (c) { return "<td>" + inl(c) + "</td>"; }).join("") + "</tr>"; }).join("") + "</tbody></table>");
        continue;
      }
      if (/^>\s?/.test(l)) { var q = []; while (i < L.length && /^>\s?/.test(L[i])) q.push(inl(L[i++].replace(/^>\s?/, ""))); out.push("<blockquote>" + q.join("<br>") + "</blockquote>"); continue; }
      if (/^\s*([-*]|\d+[.)])\s+/.test(l)) {
        var ol = /^\s*\d/.test(l), li = [];
        while (i < L.length && /^\s*([-*]|\d+[.)])\s+/.test(L[i])) li.push("<li>" + inl(L[i++].replace(/^\s*([-*]|\d+[.)])\s+/, "")) + "</li>");
        out.push((ol ? "<ol>" : "<ul>") + li.join("") + (ol ? "</ol>" : "</ul>")); continue;
      }
      var p = []; while (i < L.length && L[i].trim() && !/^(#{1,4}\s|>|\s*([-*]|\d+[.)])\s|\s*\|)/.test(L[i])) p.push(inl(L[i++]));
      if (!p.length) p.push(inl(L[i++]));
      out.push("<p>" + p.join("<br>") + "</p>");
    }
    return out.join("");
  }

  /* ---- 渲染 ---- */
  function build() {
    var pane = document.createElement("aside");
    pane.className = "ask-pane"; pane.id = "askPane";
    pane.innerHTML =
      '<div class="ask-rz" id="askRz" title="拖动调整宽度"></div>' +
      '<div class="ask-hd"><span class="ttl">' + spark(15) + "投资Buddy</span>" +
      '<span class="ctx" id="askCtx"></span><span class="sp"></span>' +
      '<button class="ask-ib" id="askNew" title="新对话">' + I_PLUS + "</button>" +
      '<button class="ask-ib" id="askHide" title="收起">' + I_SIDE + "</button></div>" +
      '<div class="ask-body" id="askBody"></div>' +
      '<div class="ask-ft"><div class="ask-box" id="askBox"><div class="ask-att" id="askAtt"></div><textarea id="askIn" rows="1" placeholder="发消息…（Enter 发送）"></textarea>' +
      '<input type="file" id="askFile" multiple hidden accept="image/png,image/jpeg,image/webp,image/gif,.pdf,.doc,.docx,.rtf,.xlsx,.xlsm,.csv,.tsv,.txt,.md,.markdown,.json,.log,.html,.xml,.yaml,.yml">' +
      '<div class="ask-row"><button class="ask-web ask-clip" id="askClip" type="button" title="独立版暂不支持图片/文件附件" hidden>' + I_CLIP + '</button><button class="ask-web" id="askWeb" type="button">' + I_GLOBE + '<span>联网</span></button><button class="ask-web ask-mode" id="askMode" type="button">' + I_STEPS + '<span>深度</span></button><span class="scope" id="askScope"></span><button class="ask-send" id="askSend" title="发送">' + I_UP + "</button></div></div>" +
      '<div class="ask-note">基于本机面板资料作答，深度模式会逐步查证；联网开启时仅搜索公开实体（持仓数量/成本永不外发）。提问内容会发送给设置中的模型接口 · 不构成投资建议</div></div>' +
      '<div class="ask-drop" id="askDrop" hidden></div>';
    document.body.appendChild(pane);
    var fab = document.createElement("button");
    fab.className = "ask-fab"; fab.id = "askFab"; fab.innerHTML = spark(15) + "投资Buddy";
    document.body.appendChild(fab);

    var ui = {}; try { ui = JSON.parse(localStorage.getItem(LS_UI) || "{}"); } catch (e) {}
    A.ui = ui;
    if (ui.w) document.documentElement.style.setProperty("--ask-w", clampW(ui.w) + "px");
    if (ui.off) document.body.classList.add("ask-off");
    window.addEventListener("resize", function () { if (ui.w) document.documentElement.style.setProperty("--ask-w", clampW(ui.w) + "px"); });
    function saveUi() { try { localStorage.setItem(LS_UI, JSON.stringify(ui)); } catch (e) {} }

    $("askHide").onclick = function () { document.body.classList.add("ask-off"); ui.off = 1; saveUi(); };
    fab.onclick = function () { document.body.classList.remove("ask-off"); ui.off = 0; saveUi(); setTimeout(function () { $("askIn").focus(); }, 30); };
    $("askNew").onclick = function () { if (A.busy) stop(); A.msgs = []; save(); render(); $("askIn").focus(); };
    var inp = $("askIn");
    inp.addEventListener("input", function () { grow(); sendState(); });
    inp.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
    });
    $("askSend").onclick = function () { A.busy ? stop() : submit(); };
    $("askWeb").onclick = function () {
      if (ui.web === "off") {
        ui.web = "on";
        toast("联网已开启：搜索词只含公开实体（公司/行业），持仓数量与成本不会外发");
      } else {
        ui.web = "off";
      }
      saveUi(); webState();
    };
    $("askMode").onclick = function () {
      ui.mode = { auto: "deep", deep: "fast", fast: "auto" }[modeVal()];
      saveUi(); modeState();
    };
    webState(); modeState();
    /* 附件：按钮 / 粘贴 / 拖拽 */
    $("askClip").onclick = function () { $("askFile").click(); };
    $("askFile").addEventListener("change", function () { addFiles(this.files); this.value = ""; });
    inp.addEventListener("paste", function (e) {
      var fs = []; var it = (e.clipboardData && e.clipboardData.items) || [];
      for (var j = 0; j < it.length; j++) if (it[j].kind === "file") { var f = it[j].getAsFile(); if (f) fs.push(f); }
      if (fs.length) { e.preventDefault(); addFiles(fs); }
    });
    var dz = 0, hasF = function (e) { var t = e.dataTransfer && e.dataTransfer.types; return t && Array.prototype.indexOf.call(t, "Files") >= 0; };
    pane.addEventListener("dragenter", function (e) { if (!hasF(e)) return; e.preventDefault(); dz++; pane.classList.add("drag"); });
    pane.addEventListener("dragover", function (e) { if (!hasF(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
    pane.addEventListener("dragleave", function (e) { if (!hasF(e)) return; dz = Math.max(0, dz - 1); if (!dz) pane.classList.remove("drag"); });
    pane.addEventListener("drop", function (e) { if (!hasF(e)) return; e.preventDefault(); dz = 0; pane.classList.remove("drag"); addFiles(e.dataTransfer.files); });
    $("askAtt").addEventListener("click", function (e) {
      var x = e.target.closest("[data-att-del]"); if (!x) return;
      A.pend.splice(+x.getAttribute("data-att-del"), 1); attState();
    });
    $("askBody").addEventListener("click", function (e) {
      var sm = e.target.closest(".ask-tools > summary");
      if (sm) { var mi = sm.closest(".ask-msg"), mm = mi && A.msgs[+mi.getAttribute("data-i")]; if (mm) mm.toolsOpen = !sm.parentNode.open; return; }
      var s = e.target.closest(".ask-sug"); if (s) { inp.value = s.getAttribute("data-q"); submit(); return; }
      var fu = e.target.closest("[data-ask-fu]"); if (fu) { inp.value = fu.getAttribute("data-ask-fu"); submit(); return; }
      var c = e.target.closest("[data-ask-copy]");
      if (c) { var m = A.msgs[+c.getAttribute("data-ask-copy")]; if (m && navigator.clipboard) navigator.clipboard.writeText(m.content).then(function () { W().toast && W().toast("已复制"); }); return; }
      var okB = e.target.closest("[data-act-ok]"); if (okB) { actConfirm(+okB.getAttribute("data-act-ok"), true); return; }
      var noB = e.target.closest("[data-act-no]"); if (noB) { actConfirm(+noB.getAttribute("data-act-no"), false); return; }
      var r = e.target.closest("[data-ask-retry]");
      if (r && !A.busy) { var k = +r.getAttribute("data-ask-retry"); A.msgs = A.msgs.slice(0, k); ask(); }
    });
    /* 拖拽宽度 */
    var rz = $("askRz");
    rz.addEventListener("pointerdown", function (e) {
      e.preventDefault(); rz.setPointerCapture(e.pointerId);
      var mv = function (ev) { var w = clampW(window.innerWidth - ev.clientX); ui.w = w; document.documentElement.style.setProperty("--ask-w", w + "px"); };
      document.body.classList.add("ask-resizing");
      var up = function () { rz.removeEventListener("pointermove", mv); rz.removeEventListener("pointerup", up); document.body.classList.remove("ask-resizing"); saveUi(); };
      rz.addEventListener("pointermove", mv); rz.addEventListener("pointerup", up);
    });
  }
  /* 拖宽上限：保证中间内容区 ≥ 680px，避免主区被挤压错乱 */
  function clampW(w) {
    var sb = document.querySelector(".sidebar"), sw = sb ? sb.getBoundingClientRect().width : 0;
    var max = Math.max(320, Math.min(900, window.innerWidth - sw - 680));
    return Math.round(Math.max(320, Math.min(max, w)));
  }
  function webState() {
    var b = $("askWeb"), on = A.ui.web !== "off";
    b.className = "ask-web" + (on ? " on" : "");
    b.title = on ? "联网：开（模型按需搜索网页/个股资讯；搜索词仅含公开实体，持仓数据不外发，点击关闭）" : "联网：关（仅用面板资料，点击开启）";
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }
  /* 三态：自动（默认，后端按问题复杂度判定）→ 深度 → 快速 */
  function modeVal() { return A.ui.mode || "auto"; }
  function modeOn() {
    var r = A.react; if (r && !r.available) return false;
    var m = modeVal();
    return m === "deep" || m === "auto";
  }
  function modeState() {
    var b = $("askMode"); if (!b) return;
    var r = A.react, m = modeVal();
    b.style.display = r && !r.available ? "none" : "";
    b.className = "ask-web ask-mode" + (m !== "fast" ? " on" : "");
    b.querySelector("span").textContent = { auto: "自动", deep: "深度", fast: "快速" }[m];
    b.title = m === "auto" ? "自动（默认）：简单问题直接答，复杂问题自动调用工具逐步查证（点击切换深度）"
      : m === "deep" ? "深度（ReAct）：调用行情/财报/研报/计算等工具逐步查证（点击切换快速）"
      : "快速：直接基于面板资料作答（点击切换自动）";
    b.setAttribute("aria-pressed", m !== "fast" ? "true" : "false");
  }
  function scopeText() {
    var el = $("askScope"); if (!el) return;
    var c = A.ctx[A.code];
    if (!c || !c.ok) { el.textContent = "上下文：" + ctxName() + (A.code === "portfolio" ? " · 全部持仓与关注" : " · 读取中…"); el.title = ""; if (!c && c !== 0) loadCtx(A.code); return; }
    var wan = c.chars >= 10000 ? (c.chars / 10000).toFixed(1) + " 万字" : c.chars + " 字";
    var cut = c.sections.filter(function (s) { return s.cut || s.drop; }).length;
    el.textContent = "上下文：" + ctxName() + " · " + c.sections.length + " 类资料 · " + wan + (cut ? " · " + cut + " 类截断" : "");
    el.title = "本轮会把以下资料放入模型上下文（上限 " + c.limit + " 字，可在 设置 → 问答与搜索 调整）：\n" +
      c.sections.map(function (s) { return "· " + s.k + "  " + s.chars + " 字" + (s.drop ? "（未放入）" : s.cut ? "（截断，原 " + s.full + "）" : ""); }).join("\n");
  }
  function loadCtx(code) {
    A.ctx[code] = 0;
    fetch("/api/ask/context?code=" + encodeURIComponent(code)).then(function (r) { return r.json(); }).then(function (d) {
      A.ctx[code] = d && d.ok ? d : { ok: false };
      if (A.code === code) scopeText();
      setTimeout(function () { delete A.ctx[code]; }, 120000);
    }, function () { delete A.ctx[code]; });
  }
  function toolsHtml(m) {
    var ts = m.tools || [], ss = m.steps || [], ck = m.check;
    if (!ts.length && !ss.length && !ck) return "";
    var run = ts.filter(function (t) { return t.status === "run"; });
    var att = ts.filter(function (t) { return t.kind === "attach"; });
    var web = ts.filter(function (t) { return WEBT[t.name]; }), pan = ts.filter(function (t) { return !WEBT[t.name] && t.kind !== "attach"; });
    var hits = web.reduce(function (a, t) { return a + (t.name === "read_url" ? 0 : (t.n || 0)); }, 0);
    var react = !!(m.react || ss.length || pan.length || ck);
    var sum;
    if (run.length) sum = '<span class="ask-spin"></span>正在' + (TOOLN[run[0].name] || "检索") + (run[0].arg ? "「" + esc(run[0].arg) + "」" : "") + "…" + (run.length > 1 ? " 等 " + run.length + " 项" : "");
    else if (ck === "run") sum = '<span class="ask-spin"></span>正在自检：核对数字出处与计算…';
    else if (!react && !att.length) sum = "已联网检索 " + ts.length + " 次" + (hits ? " · " + hits + " 条结果" : "");
    else {
      var parts = [];
      if (att.length) { var af = att.filter(function (t) { return t.status === "err"; }).length; parts.push("读取附件 " + att.length + " 个" + (af ? "（" + af + " 个失败）" : "")); }
      if (ss.length) parts.push("推理 " + ss.length + " 步");
      if (pan.length) parts.push("面板数据 " + pan.length + " 次");
      if (web.length) parts.push((react ? "" : "") + "已联网检索 " + web.length + " 次" + (hits ? " · " + hits + " 条" : ""));
      if (ck === "pass") parts.push("自检通过"); else if (ck === "fix") parts.push("自检已修订");
      sum = parts.join(" · ") || "已完成";
    }
    function row(t) {
      var ok = t.status === "run" ? "进行中" : t.status === "err" ? "失败" + (t.error ? "：" + esc(t.error) : "")
        : t.kind === "attach" ? (t.n || 0) + " 字" + (t.name === "vision" ? " · 豆包" : "")
        : WEBT[t.name] ? (t.name === "read_url" ? "已读取" : (t.n || 0) + " 条") + (t.provider ? " · " + esc(PROV[t.provider] || t.provider) : "") : "已获取";
      var head = '<div class="tq"><b>' + (TOOLN[t.name] || esc(t.name)) + "</b>" + (t.arg ? "<span>" + esc(t.arg) + "</span>" : "") +
        "<em>" + ok + "</em></div>";
      var links = (t.results || []).map(function (r) {
        if (!/^https?:\/\//.test(r.url || "")) return "";
        return '<a href="' + esc(r.url) + '" target="_blank" rel="noopener noreferrer"><span>' + esc(r.title || r.url) + "</span><i>" + esc([r.site, r.date].filter(Boolean).join(" · ")) + "</i></a>";
      }).join("");
      return '<div class="ti' + (t.status === "err" ? " err" : "") + (t.kind === "attach" ? " att" : WEBT[t.name] ? " web" : " pan") + '">' + head + (links ? '<div class="tl">' + links + "</div>" : "") + "</div>";
    }
    var rows;
    if (!ss.length) rows = ts.map(row).join("");
    else {
      rows = att.map(row).join("") + ss.map(function (s) {
        var mine = ts.filter(function (t) { return t.step === s.n; });
        return '<div class="ask-step' + (s.kind === "check" ? " ck" : "") + '"><div class="sh"><i>' + s.n + "</i><b>" + (s.kind === "check" ? "自检核实" : s.text ? "思考" : "查证") + "</b></div>" +
          (s.text ? '<div class="sx">' + esc(s.text) + "</div>" : "") + mine.map(row).join("") + "</div>";
      }).join("") + ts.filter(function (t) { return t.kind !== "attach" && !ss.some(function (s) { return s.n === t.step; }); }).map(row).join("");
    }
    if (ck && ck !== "run") rows += '<div class="ask-step ck done"><div class="sh"><i>' + (ck === "pass" ? '<svg width="9" height="9" viewBox="0 0 10 10" fill="none"><path d="M2 5.2 4.2 7.3 8 2.8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>' : "!") + "</i><b>" + (ck === "pass" ? "自检通过：数字、计算与口径已核对" : "自检发现问题，正文已替换为修订版") + "</b></div></div>";
    return '<details class="ask-tools' + (react ? " react" : "") + '"' + (m.toolsOpen ? " open" : "") + "><summary>" + (react ? I_STEPS : I_GLOBE) + "<span>" + sum + "</span>" + I_CHEV + "</summary>" + rows + "</details>";
  }
  /* ---- 附件 ---- */
  function toast(s) { if (W() && W().toast) W().toast(s); }
  function kb(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }
  function readAs(f, how) { return new Promise(function (ok, no) { var r = new FileReader(); r.onload = function () { ok(r.result); }; r.onerror = function () { no(r.error); }; r[how](f); }); }
  function scaleImg(src, edge, q) {
    return new Promise(function (ok, no) {
      var im = new Image();
      im.onload = function () {
        var w = im.naturalWidth, h = im.naturalHeight, s = Math.min(1, edge / Math.max(w, h));
        var c = document.createElement("canvas"); c.width = Math.max(1, Math.round(w * s)); c.height = Math.max(1, Math.round(h * s));
        var g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height); g.drawImage(im, 0, 0, c.width, c.height);
        ok({ url: c.toDataURL("image/jpeg", q), w: w, h: h });
      };
      im.onerror = function () { no(new Error("图片无法解码")); };
      im.src = src;
    });
  }
  function addFiles(list) {
    var fs = Array.prototype.slice.call(list || []);
    fs.forEach(function (f, j) {
      if (A.pend.length >= MAX_ATT) { if (j === 0 || A.pend.length === MAX_ATT) toast("每次最多 " + MAX_ATT + " 个附件"); return; }
      if (f.size > MAX_MB * 1048576) { toast((f.name || "文件") + " 超过 " + MAX_MB + "MB"); return; }
      var isImg = /^image\/(png|jpe?g|webp|gif)$/i.test(f.type || "");
      var name = f.name && f.name !== "image.png" ? f.name : (isImg ? "截图 " + new Date().toTimeString().slice(0, 5) + ".png" : "文件");
      if (!isImg && !TEXT_EXT.test(name) && !BIN_EXT.test(name)) { toast("暂不支持该类型：" + name); return; }
      var a = { kind: isImg ? "image" : "file", name: name, size: f.size, busy: true };
      A.pend.push(a); attState();
      var p = isImg
        ? readAs(f, "readAsDataURL").then(function (du) { return Promise.all([scaleImg(du, IMG_EDGE, 0.9), scaleImg(du, THUMB_EDGE, 0.72)]); })
            .then(function (r) { a.data = r[0].url; a.thumb = r[1].url; a.dim = r[0].w + "×" + r[0].h; })
        : TEXT_EXT.test(name) ? readAs(f, "readAsText").then(function (t) { a.text = t; })
        : readAs(f, "readAsDataURL").then(function (du) { a.data = du; });
      p.then(function () { a.busy = false; attState(); }, function (e) { A.pend.splice(A.pend.indexOf(a), 1); attState(); toast("读取失败：" + (e && e.message || name)); });
    });
    if (fs.length && A.vision && !A.vision.has_key && fs.some(function (f) { return /^image\//.test(f.type || ""); })) toast("未配置豆包 Key：图片无法识别（设置 → 问答与搜索）");
  }
  function chip(a, i, del) {
    var ext = (String(a.name).split(".").pop() || "").toUpperCase().slice(0, 4);
    var inner = a.kind === "image" && a.thumb ? '<img src="' + esc(a.thumb) + '" alt="">' : '<span class="fi">' + I_DOC + "<em>" + esc(ext) + "</em></span>";
    var sub = a.busy ? "读取中…" : a.kind === "image" ? "图片" + (a.dim ? " · " + a.dim : "") : kb(a.size || 0);
    return '<div class="ask-chip' + (a.kind === "image" ? " img" : "") + (a.busy ? " busy" : "") + '" title="' + esc(a.name) + '">' + inner +
      '<span class="nm"><b>' + esc(a.name) + "</b><small>" + sub + "</small></span>" +
      (del ? '<button type="button" class="x" data-att-del="' + i + '" title="移除">' + I_X + "</button>" : "") + "</div>";
  }
  function attState() {
    var el = $("askAtt"); if (!el) return;
    el.innerHTML = A.pend.map(function (a, i) { return chip(a, i, true); }).join("");
    el.style.display = A.pend.length ? "" : "none";
    sendState();
  }
  function grow() { var t = $("askIn"); t.style.height = "auto"; t.style.height = Math.min(160, t.scrollHeight) + "px"; }
  function sendState() {
    var b = $("askSend");
    b.className = "ask-send" + (A.busy ? " stop" : ($("askIn").value.trim() || A.pend.length) && !A.pend.some(function (a) { return a.busy; }) ? " on" : "");
    b.innerHTML = A.busy ? I_STOP : I_UP; b.title = A.busy ? "停止" : "发送";
  }
  function ctxName() { var s = A.code !== "portfolio" && W().findStock(A.code); return s ? s.name : "我的组合"; }

  function msgHtml(m, i) {
    if (m.role === "user") {
      var at = (m.atts || []).length ? '<div class="ask-uatt">' + m.atts.map(function (a, j) { return chip(a, j, false); }).join("") + "</div>" : "";
      return '<div class="ask-msg u">' + at + '<div class="bb">' + esc(m.content) + "</div></div>";
    }
    var running = (m.tools || []).some(function (t) { return t.status === "run"; });
    var body = m.error ? '<div class="err">' + esc(m.error) + "</div>"
      : m.content ? md(m.content) : running || m.check === "run" ? "" : '<span class="ask-dots"><i></i><i></i><i></i><em>' + ((m.tools || []).length ? (m.react ? "正在根据查到的数据推理…" : "正在整理检索结果…") : m.thinking ? "正在思考…" : "正在读取面板资料…") + "</em></span>";
    var ft = m.pending ? "" : '<div class="ft">' +
      (m.content ? '<button class="ask-ib" data-ask-copy="' + i + '" title="复制">' + I_COPY + "</button>" : "") +
      '<button class="ask-ib" data-ask-retry="' + i + '" title="重新生成">' + I_RETRY + "</button>" +
      (m.model ? "<span>" + esc(m.model) + (m.ms ? " · " + (m.ms / 1000).toFixed(1) + "s" : "") + "</span>" : "") + "</div>";
    return '<div class="ask-msg a" data-i="' + i + '">' + toolsHtml(m) + '<div class="bb' + (m.pending && m.content ? " ask-caret" : "") + '">' + body + "</div>" + actHtml(m, i) + ft + "</div>";
  }
  /* ---- 动作确认卡（投资Buddy「能问能办」）：模型产意图，用户确认才执行 ---- */
  function actHtml(m, i) {
    var acts = (m.actions || []).filter(function (a) { return a && a.text; });
    if (!acts.length) return "";
    var open = acts.some(function (a) { return a.state === "pending" && !a.auto; }) && !acts.some(function (a) { return a.state === "running"; });
    return '<div class="ask-act' + (open ? "" : " done") + '"><div class="t">' + (open ? "我可以帮你执行：" : "操作结果") + "</div>" +
      acts.map(function (a, j) {
        var st = a.state === "done" ? ' <span class="ok">✓ 已执行</span>'
          : a.state === "cancelled" ? ' <span class="no">已取消</span>'
          : a.state === "running" ? ' <span class="no" style="color:var(--ink-3)">执行中…</span>'
          : a.state === "failed" ? ' <span class="no">✗ ' + esc(a.error || "失败") + "</span>" : "";
        return '<div class="row">' + (j + 1) + ". " + esc(a.text) + (a.cost ? '<em class="cost">' + esc(a.cost) + "</em>" : "") + st + "</div>";
      }).join("") +
      (open ? '<div class="ops"><button class="okb" data-act-ok="' + i + '">确认执行</button><button class="nob" data-act-no="' + i + '">取消</button></div>' : "") + "</div>";
  }
  function i_cur() { return A.msgs.length - 1; }
  /* kind → 业务 API（payload 已由服务端校验；预警按现价换算触发价） */
  function alertBody(p) {
    var st = window.WB && WB.findStock(p.code), live = st && (WB.state().quotes || {})[st.gtimg];
    var b = { op: "add", code: p.code, note: "小管家确认卡创建" };
    if (p.kind === "below_pct" || p.kind === "above_pct") {
      if (!live || live.price == null) return { error: "暂无实时行情，稍后再试" };
      var px = live.price * (p.kind === "below_pct" ? 1 - p.value / 100 : 1 + p.value / 100);
      b.type = p.kind === "below_pct" ? "price_below" : "price_above";
      b.value = +px.toFixed(px >= 100 ? 1 : px >= 1 ? 2 : 3);
    } else if (p.kind === "day_abs") { b.type = "day_change_abs"; b.value = p.value; }
    else if (p.kind === "ma_below" || p.kind === "ma_above") { b.type = "ma_break"; b.value = Math.round(p.value); b.dir = p.kind === "ma_above" ? "above" : "below"; }
    else { b.type = "drawdown_from_high"; b.value = p.value; b.window = 250; }
    return b;
  }
  var ACT_EXEC = {
    add_watch: function (p) { return W().apiPost("/api/holdings", p); },
    remove_watch: function (p) { return W().apiPost("/api/holdings", p); },
    add_alert: function (p) { var b = alertBody(p); return b.error ? Promise.resolve({ ok: false, error: b.error }) : W().apiPost("/api/alerts", b); },
    add_note: function (p) { return W().apiPost("/api/notes", p); },
    add_plan: function (p) { return W().apiPost("/api/plans", p); },
    refresh: function () { return W().apiPost("/api/refresh", {}); },
    run_job: function (p) { return W().apiPost("/api/jobs/" + p.name, {}); },
    generate_expert: function (p) { return W().apiPost("/api/expert", p); },
    deep_diligence: function (p) { return W().apiPost("/api/analyze/" + p.code, {}); },
    edit_note: function (p) { return W().apiPost("/api/notes", p); },
    delete_note: function (p) { return W().apiPost("/api/notes", p); },
    update_plan: function (p) { return W().apiPost("/api/plans", p); },
    delete_plan: function (p) { return W().apiPost("/api/plans", p); },
    write_thesis: function (p) { return W().apiPost("/api/theses", p); },
    falsify_thesis: function (p) { return W().apiPost("/api/theses", p); },
    delete_thesis: function (p) { return W().apiPost("/api/theses", p); },
    record_trade: function (p) { return W().apiPost("/api/trades", p); },
    remove_position: function (p) { return W().apiPost("/api/holdings", p); },
    refresh_stock: function (p) { return W().apiPost("/api/refresh/stock", p); },
    delete_alert: function (p) { return W().apiPost("/api/alerts", p); },
    refresh_valuation: function (p) { return fetch("/api/valuation?code=" + encodeURIComponent(p.code) + "&force=1").then(function (r) { return r.json(); }); },
    set_cash: function (p) {
      var b = { op: "set_balance", currencies: p.currencies, as_of: p.as_of || "" };
      return fetch("/api/cash", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })
        .then(function (r) { return r.json(); })
        .then(function (r) {
          if (!r || !r.ok) return r;
          var h = p.realized || {};
          if (h.CNY || h.HKD || h.USD) {
            return fetch("/api/cash", { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ op: "set_realized", realized: h }) }).then(function (r2) { return r2.json(); }).then(function (r2) { return r2 && r2.ok ? r : r2; });
          }
          return r;
        });
    },
    add_cashflow: function (p) { return fetch("/api/cash", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(p) }).then(function (r) { return r.json(); }); },
  };
  function execAction(a, mi) {
    var fn = ACT_EXEC[a.kind];
    if (!fn) { a.state = "failed"; a.error = "未知动作"; return Promise.resolve(); }
    a.state = "running"; render();  /* 点击后立即反馈：按钮禁用，行内显示「执行中…」 */
    return fn(a.payload).then(function (r) {
      a.state = r && r.ok ? "done" : "failed"; a.error = r && r.error;
      if (a.state === "done" && window.WB) {
        /* 动作可能改了持仓/关注/计划/笔记/流水：重新拉取持仓再重画（修复 2026-10-19：执行后列表不刷新） */
        var done = function () {
          /* 现金变动后组合总览的「总资产/收益率」要重算，直接换掉 diag 再重画 */
          if (a.kind === "set_cash" || a.kind === "add_cashflow") {
            fetch("/api/portfolio").then(function (r) { return r.json(); })
              .then(function (d) { if (d && d.ok) { WB.state().diag = d; WB.renderMain(); } }).catch(function () {});
          }
          /* 若当前页面正是被删除/移除的标的，跳回组合总览，避免「未找到标的」空态 */
          var st = WB.state();
          if (st.selected !== "overview" && WB.findStock && !WB.findStock(st.selected)) { st.selected = "overview"; st.tab = "ov-signal"; }
          WB.renderSidebar(); WB.renderMain();
        };
        if (WB.refreshHoldings) { WB.refreshHoldings().then(done, done); } else { done(); }
      }
      render();
    }).catch(function (e) { a.state = "failed"; a.error = String(e && e.message || e); render(); });
  }
  function actConfirm(i, ok) {
    var m = A.msgs[i]; if (!m || !m.actions) return;
    if (!ok) { m.actions.forEach(function (a) { if (a.state === "pending") a.state = "cancelled"; }); render(); return; }
    var pend = m.actions.filter(function (a) { return a.state === "pending" && !a.auto; });
    var texts = [];
    pend.reduce(function (p0, a) {
      return p0.then(function () {
        return execAction(a, i).then(function () { texts.push((a.state === "done" ? "✓ " : "✗ ") + a.text); });
      });
    }, Promise.resolve()).then(function () {
      render();
      toast(texts.join("；"));
    });
  }
  function render() {
    var body = $("askBody"); if (!body) return;
    $("askCtx").textContent = ctxName();
    scopeText();
    if (!A.msgs.length) {
      var sg = A.sug[A.code];
      if (!sg) { loadSug(A.code); sg = { title: ctxName() + (A.code === "portfolio" ? " · 全局问答" : " · 深度研究"), subtitle: "正在准备推荐问题…", questions: [] }; }
      body.innerHTML = '<div class="ask-empty"><div class="ask-hero">' + spark(26) + "<h3>" + esc(sg.title) + "</h3><p>" + esc(sg.subtitle) + "</p></div>" +
        '<div class="ask-sugs">' + sg.questions.map(function (q) { return '<button class="ask-sug" data-q="' + esc(q) + '">' + esc(q) + "</button>"; }).join("") + "</div></div>";
    } else {
      body.innerHTML = A.msgs.map(msgHtml).join("");
      body.scrollTop = body.scrollHeight;
    }
    sendState();
  }
  function renderLast() {
    var body = $("askBody"), n = body.querySelectorAll(".ask-msg");
    var stick = body.scrollHeight - body.scrollTop - body.clientHeight < 60;
    var last = n[n.length - 1], i = A.msgs.length - 1;
    if (!last) return render();
    var tmp = document.createElement("div"); tmp.innerHTML = msgHtml(A.msgs[i], i);
    body.replaceChild(tmp.firstChild, last);
    if (stick) body.scrollTop = body.scrollHeight;
  }
  function loadSug(code) {
    if (A.sug[code] === 0) return; A.sug[code] = 0;
    fetch("/api/ask/suggest?code=" + encodeURIComponent(code)).then(function (r) { return r.json(); }).then(function (d) {
      A.sug[code] = d && d.ok ? d : { title: ctxName(), subtitle: "有什么想问的？", questions: [] };
      if (d && d.model) A.model = d.model;
      if (d && d.react) { A.react = d.react; modeState(); }
      if (d && d.vision) A.vision = d.vision;
      if (A.code === code && !A.msgs.length) render();
    }, function () { delete A.sug[code]; });
  }

  /* ---- 提问 / 流式 ---- */
  function submit() {
    var t = $("askIn").value.trim();
    if (A.busy || (!t && !A.pend.length)) return;
    if (A.pend.some(function (a) { return a.busy; })) { toast("附件还在读取，请稍候"); return; }
    var um = { role: "user", content: t || (A.pend.some(function (a) { return a.kind === "image"; }) ? "请解读这些图片中的信息，并结合面板资料分析对这只股票 / 组合的影响。" : "请总结这些文件的要点，并结合面板资料分析对这只股票 / 组合的影响。") };
    if (A.pend.length) {
      um.atts = A.pend.map(function (a) { return { kind: a.kind, name: a.name, size: a.size, thumb: a.thumb, dim: a.dim }; });
      um._data = A.pend.map(function (a) { return { kind: a.kind, name: a.name, data: a.data, text: a.text }; });
      A.pend = []; attState();
    }
    A.msgs.push(um);
    $("askIn").value = ""; grow();
    ask();
  }
  function stop() { if (A.ctrl) A.ctrl.abort(); }
  function ask() {
    var code = A.code;
    var hist = A.msgs.filter(function (m) { return !m.error && m.content; }).map(function (m) { return m.att ? { role: m.role, content: m.content, att: m.att } : { role: m.role, content: m.content }; });
    var um = null; for (var j = A.msgs.length - 1; j >= 0; j--) if (A.msgs[j].role === "user") { um = A.msgs[j]; break; }
    /* 本轮附件：已识别过（重试）直接带识别文本；否则上传原数据 */
    var atts = um && um.atts && !um.att && um._data ? um._data : [];
    if (um && um.atts && !um.att && !um._data) hist[hist.length - 1].content += "\n（原附件已不在本机缓存中，无法重新识别）";
    var m = { role: "assistant", content: "", pending: true };
    A.msgs.push(m); A.busy = true; render();
    var ctrl = new AbortController(); A.ctrl = ctrl;
    fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: code, messages: hist, attachments: atts, web: A.ui.web === "off" ? "off" : "on", mode: modeVal() }), signal: ctrl.signal })
      .then(function (res) {
        if (!res.ok || !res.body) return res.json().then(function (j) { throw new Error(j.error || ("HTTP " + res.status)); });
        var rd = res.body.getReader(), dec = new TextDecoder(), buf = "";
        function pump() {
          return rd.read().then(function (r) {
            if (r.done) return;
            buf += dec.decode(r.value, { stream: true });
            var parts = buf.split("\n\n"); buf = parts.pop();
            parts.forEach(function (p) {
              var line = p.replace(/^data:\s?/, ""); if (!line) return;
              var d; try { d = JSON.parse(line); } catch (e) { return; }
              if (d.meta) { m.model = d.meta.model; m.react = !!d.meta.react; if (d.meta.mode_auto) m.modeAuto = d.meta.mode_auto; }
              if (d.round != null) m._r0 = m.content.length;
              if (d.step) { m.steps = m.steps || []; m.steps.push(d.step); if (d.clear) m.content = m.content.slice(0, m._r0 || 0); }
              if (d.check) m.check = d.check.status;
              if (d.attach && d.attach.results && um) { um.att = String(d.attach.block || "").slice(0, 40000); um.attRes = d.attach.results; if (um.att) delete um._data; }
              if (typeof d.replace === "string") m.content = d.replace;
              if (d.thinking) m.thinking = true;
              if (d.action) {  /* 动作确认卡（模型意图 → 用户确认才执行） */
                m.actions = m.actions || [];
                var _ac = { kind: d.action.kind, text: d.action.text, payload: d.action.payload,
                            cost: d.action.cost, auto: d.action.auto, state: "pending" };
                m.actions.push(_ac);
                if (_ac.auto) execAction(_ac, i_cur());
              }
              if (d.tool) {
                m.tools = m.tools || [];
                var k = -1; m.tools.forEach(function (t, j) { if (t.id === d.tool.id) k = j; });
                if (k < 0) m.tools.push(d.tool); else m.tools[k] = d.tool;
              }
              if (d.t) m.content += d.t;
              if (d.error) m.error = d.error + (m.content ? "" : "");
              if (d.verify && d.verify.unverified && d.verify.unverified.length) m.unverified = d.verify.unverified;
              if (d.done) { m.ms = d.ms; m.model = d.model; m.followups = d.followups; }
            });
            if (A.code === code) renderLast();
            return pump();
          });
        }
        return pump();
      })
      .catch(function (e) { if (ctrl.signal.aborted) { if (!m.content) m.error = "已停止"; } else m.error = "请求失败：" + (e.message || e); })
      .then(function () {
        m.pending = false; A.busy = false; A.ctrl = null;
        if (m.error && m.content) { m.content += "\n\n> " + m.error; m.error = null; }
        if (A.code === code) { save(); render(); } else { var keep = load(code).concat([]); keep.push(um ? slim(um) : hist[hist.length - 1], { role: "assistant", content: m.content || "", model: m.model, ms: m.ms, tools: m.tools, steps: m.steps, check: m.check, react: m.react }); try { localStorage.setItem(LS + code, JSON.stringify(keep.slice(-30))); } catch (e) {} }
      });
  }

  /* ---- 跟随左侧选中标的切换上下文 ---- */
  function sync() {
    var c = curCode();
    if (c === A.code) return;
    if (A.busy) { stop(); }
    A.code = c; A.msgs = load(c); A.pend = []; attState(); render();
  }
  function init() {
    if (!window.WB) return setTimeout(init, 80);
    build(); sync();
    var main = document.getElementById("main");
    if (main && window.MutationObserver) new MutationObserver(function () { sync(); }).observe(main, { childList: true });
  }
  window.WBAsk = { md: md, state: A, sync: sync };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
