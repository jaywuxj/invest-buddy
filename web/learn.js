/* 投资学堂（2026-11-02）：系统化学习路径 + 知识卡 + 精选外链 + 「在你的组合里看」。
   内容来自 /api/learn（harness/learn_content/*.json，人工撰写、外链白名单）；进度存本机 data/learn_progress.json。
   「在你的组合里看」全部由程序用本机已加载的数据计算，不调用模型、不外发。 */
(function () {
  var W = function () { return window.WB; };
  var S = function () { return window.WB.state; };
  var L = { data: null, busy: false, err: null };

  var IC = {
    book: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 3.2c1.8-.6 3.6-.5 5.5.6v9c-1.9-1.1-3.7-1.2-5.5-.6z"/><path d="M13.5 3.2c-1.8-.6-3.6-.5-5.5.6v9c1.9-1.1 3.7-1.2 5.5-.6z"/></svg>'
  };

  function esc(s) { return W().esc(s); }
  function priv() { return !!S().priv; }
  function pp(s) { return priv() ? "****" : s; }

  function load(force) {
    if (L.busy || (L.data && !force)) return Promise.resolve(L.data);
    L.busy = true;
    return fetch("/api/learn").then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) { L.data = d; L.err = null; } else { L.err = (d && d.message) || "加载失败"; }
      return L.data;
    }).catch(function (e) { L.err = String(e && e.message || e); }).then(function (x) {
      L.busy = false;
      if (window.WB_RENDER_DOCK) window.WB_RENDER_DOCK();
      if (S().selected === "learn") W().renderMain();
      return x;
    });
  }

  function doneMap() { return (L.data && L.data.progress && L.data.progress.done) || {}; }
  function doneCount() { return Object.keys(doneMap()).length; }
  function badge() { return L.data ? doneCount() + "/" + L.data.total : ""; }

  function findPath(id) { return (L.data.paths || []).filter(function (p) { return p.id === id; })[0]; }
  function pathOfLesson(lid) {
    var hit = null;
    (L.data.paths || []).forEach(function (p) { (p.lessons || []).forEach(function (l) { if (l.id === lid) hit = p; }); });
    return hit;
  }
  function curPathLesson() {
    var st = S(), p = findPath(st.tab), dm = doneMap();
    if (!p && st.learnLesson) p = pathOfLesson(st.learnLesson);
    if (!p && L.data.progress && L.data.progress.last) p = pathOfLesson(L.data.progress.last);
    if (!p) p = L.data.paths[0];
    var ls = p.lessons || [];
    var cur = ls.filter(function (l) { return l.id === st.learnLesson; })[0];
    if (!cur && L.data.progress && L.data.progress.last) cur = ls.filter(function (l) { return l.id === L.data.progress.last; })[0];
    if (!cur) cur = ls.filter(function (l) { return !dm[l.id]; })[0] || ls[0];
    st.tab = p.id; st.learnLesson = cur.id;
    return { p: p, l: cur, i: ls.indexOf(cur) };
  }
  function setHash() {
    var st = S();
    try { history.replaceState(null, "", "#learn/" + st.tab + "/" + st.learnLesson); } catch (e) {}
  }

  /* ---------- 「在你的组合里看」：程序计算 ---------- */
  function positions() { return (S().holdings && S().holdings.positions) || []; }
  function nameOf(c) { var s = W().findStock(c); return s ? s.name : c; }
  function needDiag() {
    var st = S();
    if (st.diag || L.diagBusy) return !!st.diag;
    L.diagBusy = true;
    fetch("/api/portfolio").then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) st.diag = d;
    }).catch(function () {}).then(function () { L.diagBusy = false; if (st.selected === "learn") W().renderMain(); });
    return false;
  }
  function cell(k, v, sub, go, warn) {
    var attr = go ? ' data-ln-go="' + esc(go[0]) + '" data-ln-tab="' + esc(go[1]) + '"' : "";
    return (go ? '<button class="ln-cell' + (warn ? " warn" : "") + '"' + attr + ">" : '<div class="ln-cell' + (warn ? " warn" : "") + '">') +
      '<div class="k">' + esc(k) + '</div><div class="v">' + v + '</div>' + (sub ? '<div class="s" title="' + esc(sub) + '">' + esc(sub) + "</div>" : "") +
      (go ? "</button>" : "</div>");
  }
  function buckets(defs, valueOf, tab) {
    var groups = defs.map(function () { return []; }), miss = [];
    positions().forEach(function (p) {
      var v = valueOf(p.code);
      if (v == null || !isFinite(v)) { miss.push(p.code); return; }
      for (var i = 0; i < defs.length; i++) if (defs[i].test(v)) { groups[i].push(p.code); return; }
    });
    var html = defs.map(function (d, i) {
      var xs = groups[i];
      return cell(d.k, xs.length + " 只", xs.map(nameOf).slice(0, 4).join("、") || "—", xs.length ? [xs[0], tab] : null, d.warn && xs.length);
    }).join("");
    if (miss.length) html += cell("暂无数据", miss.length + " 只", miss.map(nameOf).slice(0, 4).join("、"), null);
    return html;
  }
  function val(code) { var v = S().valAll && S().valAll[code]; return v && v.ok ? v : null; }
  var LIVE = {
    "pe-pct": function () {
      if (!S().valAll) return null;
      return { html: buckets([
        { k: "分位 ≤20%（偏低）", test: function (v) { return v <= 20; } },
        { k: "20%–60%", test: function (v) { return v > 20 && v <= 60; } },
        { k: "分位 >60%（偏高）", test: function (v) { return v > 60; }, warn: 1 }
      ], function (c) { var v = val(c); return v && v.hist ? v.hist.rank : null; }, "valuation"), note: "按持仓股的 5 年估值分位分组，点格子打开该组第一只的估值 Tab。" };
    },
    mos: function () {
      if (!S().valAll) return null;
      return { html: buckets([
        { k: "安全边际 ≥30%", test: function (v) { return v >= 0.3; } },
        { k: "0–30%", test: function (v) { return v >= 0 && v < 0.3; } },
        { k: "为负（现价高于中性估值）", test: function (v) { return v < 0; }, warn: 1 }
      ], function (c) { var v = val(c); return v && v.dcf ? v.dcf.mos : null; }, "valuation"), note: "安全边际取反向 DCF 中性情景，与估值 Tab 同源。" };
    },
    peers: function () {
      if (!S().valAll) return null;
      return { html: buckets([
        { k: "比同行便宜 >20%", test: function (v) { return v <= -0.2; } },
        { k: "±20% 以内", test: function (v) { return v > -0.2 && v < 0.2; } },
        { k: "比同行贵 >20%", test: function (v) { return v >= 0.2; }, warn: 1 }
      ], function (c) { var v = val(c); return v && v.peers ? v.peers.prem_pe : null; }, "valuation"), note: "按市盈率相对同行中位数的溢价 / 折价分组。" };
    },
    confidence: function () {
      if (!S().valAll) return null;
      var rank = { "高": 3, "中": 2, "低": 1 };
      return { html: buckets([
        { k: "可信度高", test: function (v) { return v === 3; } },
        { k: "可信度中", test: function (v) { return v === 2; } },
        { k: "可信度低（结论封顶）", test: function (v) { return v === 1; }, warn: 1 }
      ], function (c) { var v = val(c); return v ? rank[v.confidence] : null; }, "valuation"), note: "可信度低的标的，估值结论最多到「偏贵 / 偏便宜」。" };
    },
    sizing: function () {
      if (!needDiag()) return { loading: 1 };
      var d = S().diag, R = d.rules || {}, rows = d.rows || [];
      var warn = 20, red = R.single_max || 25;
      var over = rows.filter(function (r) { return r.weight > red; }), near = rows.filter(function (r) { return r.weight > warn && r.weight <= red; });
      var top3 = rows.slice(0, 3).reduce(function (a, r) { return a + r.weight; }, 0);
      var lst = function (xs) { return xs.map(function (r) { return r.name + (priv() ? "" : " " + r.weight.toFixed(1) + "%"); }).join("、"); };
      return { html: cell("超过单股红线 " + red + "%", over.length + " 只", lst(over) || "—", over.length ? ["overview", "ov-diag"] : null, over.length) +
        cell("接近红线（" + warn + "%–" + red + "%）", near.length + " 只", lst(near) || "—", near.length ? ["overview", "ov-diag"] : null, near.length) +
        cell("前三大合计", pp(top3.toFixed(1) + "%"), "红线 " + (R.top3_max || 50) + "%", ["overview", "ov-diag"], top3 > (R.top3_max || 50)),
        note: "占比分母为总资产（股票 + 现金）。" };
    },
    hhi: function () {
      if (!needDiag()) return { loading: 1 };
      var t = S().diag.totals || {};
      return { html: cell("名义持仓", (t.count || 0) + " 只", "持仓股票数量", null) +
        cell("有效持仓数", (t.hhi_n || 0).toFixed(1) + " 只", "1 ÷ HHI", ["overview", "ov-diag"]) +
        cell("集中度折损", t.count ? Math.round((1 - (t.hhi_n || 0) / t.count) * 100) + "%" : "--", "越高说明仓位越集中在少数几只", null),
        note: "有效持仓数明显小于名义数量，说明少数几只股票决定了组合的大部分盈亏。" };
    },
    exposure: function () {
      if (!needDiag()) return { loading: 1 };
      var d = S().diag, R = d.rules || {};
      var sec = (d.by_sector || [])[0], mk = (d.by_market || [])[0];
      var fx = (d.by_currency || []).filter(function (c) { return c.name !== "CNY"; }).reduce(function (a, c) { return a + c.weight; }, 0);
      return { html: (sec ? cell("最大行业 · " + sec.name, pp(sec.weight.toFixed(1) + "%"), "红线 " + (R.sector_max || 35) + "%", ["overview", "ov-diag"], sec.weight > (R.sector_max || 35)) : "") +
        (mk ? cell("最大市场 · " + mk.name, pp(mk.weight.toFixed(1) + "%"), "红线 " + (R.market_max || 70) + "%", ["overview", "ov-diag"], mk.weight > (R.market_max || 70)) : "") +
        cell("外币资产占比", pp(fx.toFixed(1) + "%"), "外币每贬值 " + (R.fx_shock_pct || 5) + "%，这部分随之缩水", ["overview", "ov-diag"]),
        note: "行业按「风险大类」归并，口径见组合诊断。" };
    },
    funds: function () {
      if (!needDiag()) return { loading: 1 };
      var f = S().diag.funds || {}, t = S().diag.totals || {};
      if (!f.configured) return { html: cell("现金", "未录入", "去组合诊断 →「资金总账」录入现金和历史已实现盈亏", ["overview", "ov-diag"], 1), note: "未录入现金时，总资产只等于股票市值，收益率只反映浮动盈亏。" };
      return { html: cell("现金占总资产", pp((f.cash_weight || 0).toFixed(1) + "%"), "可直接用于加仓", ["overview", "ov-diag"]) +
        cell("收益率（净投入口径）", pp((t.return_pct > 0 ? "+" : "") + (t.return_pct != null ? t.return_pct.toFixed(2) : "--") + "%"), "总收益 ÷ 净投入", ["overview", "ov-diag"]) +
        cell("持仓浮动收益率", pp((t.pnl_pct > 0 ? "+" : "") + (t.pnl_pct || 0).toFixed(2) + "%"), "只看持仓，不含已实现与现金", null),
        note: "两种收益率差得越多，说明已卖出盈亏和现金对你的真实收益影响越大。" };
    },
    "deep-loss": function () {
      if (!needDiag()) return { loading: 1 };
      var tiers = S().diag.loss_tiers || [];
      return { html: tiers.map(function (x) {
        return cell(x.label, x.count + " 只", x.members.map(function (m) { return m.name; }).slice(0, 4).join("、") || "—", x.count ? ["overview", "ov-diag"] : null, x.level >= 3 && x.count);
      }).join(""), note: "浮亏越深的一档越要先看判断卡：逻辑没变才谈得上「坚持」。" };
    },
    thesis: function () {
      var th = (S().theses && S().theses.items) || {};
      var ps = positions(), has = ps.filter(function (p) { return th[p.code]; }), no = ps.filter(function (p) { return !th[p.code]; });
      return { html: cell("已写判断卡", has.length + " 只", has.map(function (p) { return p.name; }).slice(0, 4).join("、") || "—", has.length ? [has[0].code, "decide"] : null) +
        cell("还没写", no.length + " 只", no.map(function (p) { return p.name; }).slice(0, 4).join("、") || "—", no.length ? [no[0].code, "decide"] : null, no.length),
        note: "点「还没写」直接打开第一只的决策页，写一句话判断 + 证伪条件。" };
    }
  };

  /* ---------- 渲染 ---------- */
  function domain(u) { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return ""; } }
  function renderMain() {
    var st = S();
    if (!L.data) {
      if (!L.busy && !L.err) load();
      return '<div class="empty">' + (L.err ? "投资学堂加载失败：" + esc(L.err) : "正在加载投资学堂…") + "</div>";
    }
    var c = curPathLesson(), p = c.p, l = c.l, dm = doneMap();
    setHash();
    var html = '<div class="stock-sticky"><div class="stock-head"><div><h1>投资学堂</h1>' +
      '<div class="head-note">已掌握 <b>' + doneCount() + "</b> / " + L.data.total + " 课 · 学一个概念，马上在自己的组合里看到它怎么用</div></div></div>" +
      '<div class="tabs">' + L.data.paths.map(function (x) {
        return '<button class="tab' + (x.id === p.id ? " active" : "") + '" data-ln-path="' + esc(x.id) + '">' + esc(x.title) + "</button>";
      }).join("") + "</div></div>";

    var nDone = (p.lessons || []).filter(function (x) { return dm[x.id]; }).length;
    var toc = '<nav class="ln-toc"><div class="d">' + esc(p.desc || "") + " · " + nDone + "/" + p.lessons.length + '</div><div class="bar"><i style="width:' + (nDone / p.lessons.length * 100).toFixed(1) + '%"></i></div>' +
      p.lessons.map(function (x, i) {
        return '<a class="' + (x.id === l.id ? "cur " : "") + (dm[x.id] ? "done" : "") + '" data-ln-lesson="' + esc(x.id) + '"><span class="n">' + (dm[x.id] ? "✓" : i + 1) + '</span><span class="t">' + esc(x.title) + '</span><span class="m">' + (x.min || "") + "′</span></a>";
      }).join("") + "</nav>";

    var card = '<article class="ln-card"><div class="ln-meta">' + esc(p.title) + " · 第 " + (c.i + 1) + " 课 · 约 " + (l.min || 3) + " 分钟" + (dm[l.id] ? '<span class="ok">已掌握 ' + esc(dm[l.id]) + "</span>" : "") + "</div>" +
      "<h2>" + esc(l.title) + "</h2>" + (l.en ? '<div class="en">' + esc(l.en) + "</div>" : "") +
      '<div class="ln-one">' + esc(l.one) + "</div>" + (l.formula ? '<div class="ln-f">' + esc(l.formula) + "</div>" : "");
    if (l.how && l.how.length) card += '<div class="ln-sec"><div class="h">要点</div><ul>' + l.how.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul></div>";
    card += '<div class="ln-sec"><div class="h">本产品怎么用</div><p>' + esc(l.product) + "</p>" +
      (l.go ? '<button class="ln-go" data-ln-jump="1">' + esc(l.go.label || "去看看") + " ›</button>" : "") + "</div>";
    if (l.live && LIVE[l.live] && positions().length) {
      var lv = null;
      try { lv = LIVE[l.live](); } catch (e) { lv = null; }
      if (lv) card += '<div class="ln-sec"><div class="h">在你的组合里看<em>本机计算，不外发</em></div>' +
        (lv.loading ? '<p>正在读取组合数据…</p>' : '<div class="ln-live">' + lv.html + "</div>" + (lv.note ? '<div class="ln-livenote">' + esc(lv.note) + "</div>" : "")) + "</div>";
    }
    card += '<div class="ln-sec"><div class="h">常见误区</div><p>' + esc(l.pitfall) + "</p></div>";
    var links = (l.links || []).map(function (id) { return L.data.links[id]; }).filter(Boolean);
    if (links.length) {
      card += '<div class="ln-sec"><div class="h">延伸阅读<em>精选外链 · 新窗口打开</em></div><div class="ln-links">' + links.map(function (k) {
        return '<a class="ln-link" href="' + esc(k.url) + '" target="_blank" rel="noopener noreferrer"><span class="kd">' + esc(k.kind || "链接") + '</span><span class="tt">' + esc(k.title) + '</span><span class="dm">' + esc(domain(k.url)) + (k.lang === "en" ? " · 英文" : "") + " ↗</span></a>";
      }).join("") + "</div>" + (links.some(function (k) { return /wikipedia|youtube/.test(k.url); }) ? '<div class="ln-net">维基百科等境外站点在部分网络环境下可能无法打开。</div>' : "") + "</div>";
    }
    var ls = p.lessons, prev = ls[c.i - 1], next = ls[c.i + 1];
    var np = !next ? L.data.paths[L.data.paths.indexOf(p) + 1] : null;
    card += '<div class="ln-foot"><button class="pri' + (dm[l.id] ? " on" : "") + '" data-ln-done="1">' + (dm[l.id] ? "✓ 已掌握" : "标记已掌握") + "</button>" +
      '<button data-ln-ask="1">问 AI：结合我的持仓讲讲</button><span class="sp"></span>' +
      '<button class="nx" data-ln-step="-1"' + (prev ? "" : " disabled") + ">‹ " + (prev ? esc(prev.title) : "上一课") + "</button>" +
      (next ? '<button class="nx" data-ln-step="1">' + esc(next.title) + " ›</button>" : np ? '<button class="nx" data-ln-path="' + esc(np.id) + '">下一条路径：' + esc(np.title) + " ›</button>" : '<button class="nx" disabled>已是最后一课</button>') +
      "</div></article>";
    return html + '<div class="ln-wrap">' + toc + card + "</div>";
  }

  function pickStock() {
    var st = S(), c = st.learnFrom;
    if (c && W().findStock(c)) return c;
    var ps = positions(), ws = (st.holdings && st.holdings.watch) || [];
    return (ps[0] || ws[0] || {}).code;
  }
  function goTo(sel, tab) {
    var st = S();
    if (!sel) return;
    st.selected = sel; st.tab = tab;
    W().renderSidebar(); W().renderMain(); window.scrollTo(0, 0);
    try { history.replaceState(null, "", "#" + sel + "/" + tab); } catch (e) {}
    if (window.WB_RENDER_DOCK) window.WB_RENDER_DOCK();
  }
  function openLesson(id, pathId) {
    var st = S();
    if (pathId) st.tab = pathId;
    st.learnLesson = id;
    W().renderMain(); window.scrollTo(0, 0);
    fetch("/api/learn/progress", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ op: "last", id: id }) })
      .then(function (r) { return r.json(); }).then(function (r) { if (r && r.ok) L.data.progress = r.progress; }).catch(function () {});
  }
  function bind() {
    if (!L.data) return;
    var main = document.getElementById("main");
    var c = curPathLesson();
    Array.prototype.forEach.call(main.querySelectorAll("[data-ln-path]"), function (el) {
      el.addEventListener("click", function () {
        var p = findPath(el.getAttribute("data-ln-path")); if (!p) return;
        S().tab = p.id; S().learnLesson = null; W().renderMain(); window.scrollTo(0, 0);
      });
    });
    Array.prototype.forEach.call(main.querySelectorAll("[data-ln-lesson]"), function (el) {
      el.addEventListener("click", function () { openLesson(el.getAttribute("data-ln-lesson")); });
    });
    Array.prototype.forEach.call(main.querySelectorAll("[data-ln-step]"), function (el) {
      el.addEventListener("click", function () {
        var x = c.p.lessons[c.i + Number(el.getAttribute("data-ln-step"))]; if (x) openLesson(x.id);
      });
    });
    Array.prototype.forEach.call(main.querySelectorAll("[data-ln-go]"), function (el) {
      el.addEventListener("click", function () { goTo(el.getAttribute("data-ln-go"), el.getAttribute("data-ln-tab")); });
    });
    var jump = main.querySelector("[data-ln-jump]");
    if (jump) jump.addEventListener("click", function () {
      var g = c.l.go || {};
      if (g.ov) goTo("overview", g.ov);
      else if (g.stock) { var code = pickStock(); if (code) goTo(code, g.stock); else W().toast("先添加一只持仓或关注，才能跳到个股页"); }
    });
    var db = main.querySelector("[data-ln-done]");
    if (db) db.addEventListener("click", function () {
      var on = !doneMap()[c.l.id];
      db.disabled = true;
      fetch("/api/learn/progress", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ op: "done", id: c.l.id, done: on }) })
        .then(function (r) { return r.json(); }).then(function (r) {
          if (!r || !r.ok) { W().toast("保存失败：" + ((r && r.message) || "网络异常")); return; }
          L.data.progress = r.progress;
          if (on) {   /* 标记后自动进入本路径下一课；最后一课停留 */
            var nx = c.p.lessons[c.i + 1];
            if (nx) S().learnLesson = nx.id;
            W().toast(nx ? "已掌握，进入下一课" : "已掌握「" + c.p.title + "」最后一课");
          }
          if (window.WB_RENDER_DOCK) window.WB_RENDER_DOCK();
          W().renderMain();
        }).catch(function () { W().toast("保存失败：网络异常"); }).then(function () { db.disabled = false; });
    });
    var ab = main.querySelector("[data-ln-ask]");
    if (ab) ab.addEventListener("click", function () {   /* 只预填问题，由用户决定是否发送 */
      var fab = document.getElementById("askFab"); if (fab) fab.click();
      setTimeout(function () {
        var t = document.getElementById("askIn"); if (!t) return;
        t.value = "请结合我的持仓，用大白话讲讲「" + c.l.title + "」：它在我的组合里最相关的是哪一两只股票？要注意什么？";
        t.dispatchEvent(new Event("input", { bubbles: true }));
        t.focus();
      }, 60);
    });
  }

  window.LEARN = { load: load, renderMain: renderMain, bind: bind, badge: badge, icon: IC.book,
    _inject: function (d) { L.data = d; }, _live: LIVE };   /* _inject/_live 仅供离线冒烟测试 */
})();
