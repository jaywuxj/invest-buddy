/*
 * p1.js — 投资Buddy P1 功能前端（共识变化 / 纪律检查 / 交易流水与归因 / 准确率 / 估值 / 简报 / 事件评分 / 财报验证）
 * 依赖 index.html 主 IIFE 暴露的 window.WB 桥（state/esc/fmtNum/apiPost/fetchOpt/toast/renderMain...）。
 * 约定：render* 返回 HTML 字符串；bind* 在 renderMain 之后绑定事件；异步数据挂在 WB.state.p1.*。
 */
(function () {
  "use strict";
  var P1 = {};
  function W() { return window.WB; }
  function S() { var s = W().state; s.p1 = s.p1 || {}; return s.p1; }
  function esc(v) { return W().esc(v == null ? "" : v); }
  function n2(v, d) { return v == null || !isFinite(v) ? "—" : Number(v).toFixed(d == null ? 2 : d); }
  function sg(v, d) { return v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(d == null ? 1 : d); }
  function cls(v) { return v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : ""; }
  function yuan(v) {
    if (v == null || !isFinite(v)) return "—";
    if (W().state.priv) return "****";
    var a = Math.abs(v), s = v < 0 ? "−" : v > 0 ? "+" : "";
    return s + "¥" + (a >= 10000 ? (a / 10000).toFixed(2) + " 万" : Math.round(a).toLocaleString("en-US"));
  }
  function load(key, url, cb, force) {
    var st = S();
    var cur = st[key];
    // 失败结果也要缓存（冷却 60s），否则「失败 → renderMain → 再请求 → 再失败」会无限重绘，页面 tab 无法点击
    if (!force && cur && (cur.ok || cur.loading || (cur.failed_at && Date.now() - cur.failed_at < 60000))) return;
    st[key] = { loading: true };
    W().fetchOpt(url).then(function (d) {
      st[key] = d && d.ok !== false ? d : { ok: false, failed_at: Date.now(), error: (d && d.error) || "unavailable" };
      if (cb) cb(st[key]);
      if (!W().editingNow()) W().renderMain();
    }, function (e) {
      st[key] = { ok: false, failed_at: Date.now(), error: String((e && e.message) || e || "network") };
      if (cb) cb(st[key]);
    });
  }
  function byId(id) { return document.getElementById(id); }
  function on(sel, ev, fn) { Array.prototype.forEach.call(document.querySelectorAll(sel), function (el) { el.addEventListener(ev, function (e) { fn(el, e); }); }); }
  var LV = { ok: ["通过", "p1-ok"], amber: ["黄牌", "p1-amber"], red: ["红牌", "p1-red"] };
  P1.lvBadge = function (lv) { var x = LV[lv] || LV.ok; return '<span class="p1-badge ' + x[1] + '">' + x[0] + "</span>"; };


  /* ================= P1-8 交易前纪律检查 ================= */
  P1.ACTIONS = [["watch", "观察"], ["buy", "建仓"], ["add", "加仓"], ["reduce", "减仓"], ["sell", "卖出"], ["other", "其他"]];
  P1.renderDisc = function (check, reasonId) {
    if (!check) return "";
    var bad = check.items.filter(function (i) { return i.level !== "ok"; });
    var okN = check.items.length - bad.length;
    var html = '<div class="p1-disc p1-disc-' + check.level + '"><div class="p1-head">' + P1.lvBadge(check.level) +
      "<b>" + esc(check.action_label) + "纪律检查 · " + esc(check.name || check.code) + "</b>" +
      '<span class="p1-sub">' + okN + " 项通过" + (bad.length ? " · " + bad.length + " 项需说明" : "") + (check.post_weight != null ? " · 操作后仓位 " + check.post_weight + "%" : "") + "</span></div>";
    html += '<div class="p1-disc-items">' + check.items.map(function (i) {
      return '<div class="p1-di p1-di-' + i.level + '"><span class="p1-dot"></span><span class="dim">' + esc(i.dim) + "</span><b>" + esc(i.title) + '</b><span class="det">' + esc(i.detail) + "</span></div>";
    }).join("") + "</div>";
    if (check.level !== "ok" && reasonId) {
      html += '<textarea id="' + reasonId + '" class="p1-reason" maxlength="1000" placeholder="' + (check.level === "red" ? "红牌" : "黄牌") + "：写下你仍然要执行的理由（≥ " + check.min_reason + ' 字），会与决策一起存档用于复盘"></textarea>';
    }
    return html + "</div>";
  };
  P1.planFormExtra = function () {
    return '<div class="frow p1-frow"><select id="plAct" title="动作类型（建仓/加仓/减仓/卖出会触发纪律检查）">' +
      P1.ACTIONS.map(function (a) { return '<option value="' + a[0] + '">' + a[1] + "</option>"; }).join("") + "</select>" +
      '<input type="number" id="plAmt" min="0" step="any" placeholder="拟投入金额（本币，可选，用于测算仓位）">' +
      '<button class="nt-ghost" id="plChk" type="button">纪律检查</button></div><div id="plDisc"></div>';
  };
  function planCheck() {
    var act = byId("plAct") && byId("plAct").value;
    var box = byId("plDisc");
    if (!box) return;
    if (["buy", "add", "reduce", "sell"].indexOf(act) < 0) { box.innerHTML = '<div class="p1-hint">观察/其他类计划不做纪律检查。</div>'; return; }
    box.innerHTML = '<div class="p1-hint">检查中…</div>';
    W().apiPost("/api/discipline/check", { code: byId("plCode").value, action: act, amount: Number(byId("plAmt").value) || 0 }).then(function (r) {
      box.innerHTML = r.ok ? P1.renderDisc(r, "plDiscReason") : '<div class="p1-hint">检查失败：' + esc(r.error) + "</div>";
    });
  }
  P1.bindPlanForm = function () {
    if (!byId("plAct")) return;
    byId("plChk").addEventListener("click", planCheck);
    byId("plAct").addEventListener("change", planCheck);
  };
  /* 供 index.html 新建计划时拼 body */
  P1.planBodyExtra = function () {
    var r = byId("plDiscReason");
    return { action: byId("plAct") ? byId("plAct").value : "", amount: byId("plAmt") ? Number(byId("plAmt").value) || 0 : 0, discipline_reason: r ? r.value.trim() : "" };
  };
  P1.onPlanReject = function (r) {
    if (!r || r.error !== "discipline_ack_required") return false;
    var box = byId("plDisc");
    var keep = byId("plDiscReason") ? byId("plDiscReason").value : "";
    if (box) box.innerHTML = P1.renderDisc(r.check, "plDiscReason");
    if (byId("plDiscReason")) { byId("plDiscReason").value = keep; byId("plDiscReason").focus(); }
    W().toast(r.message || "需要填写理由");
    return true;
  };
  P1.planDiscTag = function (p) {
    if (!p || !p.discipline) return p && p.action ? '<span class="p1-badge p1-muted-b">' + esc(actLabel(p.action)) + "</span>" : "";
    var d = p.discipline;
    return '<span class="p1-badge p1-muted-b">' + esc(actLabel(p.action)) + "</span>" + P1.lvBadge(d.level) +
      (d.items && d.items.length ? '<span class="p1-sub" title="' + esc(d.reason || "") + '">' + esc(d.items.map(function (i) { return i.title; }).join("；")) + "</span>" : "");
  };
  function actLabel(a) { for (var i = 0; i < P1.ACTIONS.length; i++) if (P1.ACTIONS[i][0] === a) return P1.ACTIONS[i][1]; return a || ""; }

  /* ================= P1-4 交易流水与收益归因 ================= */
  P1.loadTrades = function (force) { load("trades", "/api/trades", null, force); };
  var SIDE_TXT = { init: "期初", buy: "买入", sell: "卖出", dividend: "分红", split: "拆股" };
  P1.renderTrades = function () {
    var d = S().trades;
    if (!d || d.loading) { P1.loadTrades(false); return '<div class="empty">交易流水加载中…（需拉取基准指数日 K）</div>'; }
    if (!d.ok) return '<div class="empty">交易流水不可用：' + esc(d.error) + "</div>";
    var a = d.attribution, t = a.totals;
    var html = '<div class="p1-intro">成本由流水按<b>平均成本法</b>推导并自动回写持仓；收益拆为 <b>市场</b>（同样现金流买基准）+ <b>选股</b> + <b>时机</b>（成交价 vs 前后 10 日均价）+ <b>汇率</b>，四项严格加总。</div>';
    var comp = [["总盈亏", t.pnl_cny], ["市场 β", t.market_cny], ["选股 α", t.selection_cny], ["时机", t.timing_cny], ["汇率", t.fx_cny]];
    html += '<div class="ov-chips">' + comp.map(function (c) { return '<div class="ov-chip"><div class="k">' + c[0] + '</div><div class="v ' + cls(c[1]) + '">' + yuan(c[1]) + "</div></div>"; }).join("") +
      '<div class="ov-chip"><div class="k">已实现 / 分红</div><div class="v">' + yuan(t.realized_cny) + " / " + yuan(t.dividends_cny) + "</div></div></div>";
    /* 归因条：各组件绝对值占比 */
    var parts = comp.slice(1), absSum = parts.reduce(function (s, c) { return s + Math.abs(c[1] || 0); }, 0) || 1;
    html += '<div class="p1-attrbar">' + parts.map(function (c, i) {
      var w = (Math.abs(c[1] || 0) / absSum) * 100;
      return w < 0.5 ? "" : '<div class="seg s' + i + " " + ((c[1] || 0) >= 0 ? "pos" : "neg") + '" style="width:' + w.toFixed(2) + '%" title="' + c[0] + " " + yuan(c[1]) + '"><span>' + (w > 9 ? c[0] : "") + "</span></div>";
    }).join("") + '</div><div class="p1-legend">色块宽度 = 各归因分量绝对值占比；实色为正贡献，斜线为负贡献</div>';
    if (a.drift && a.drift.length) html += '<div class="p1-warn">持仓文件与流水推导不一致：' + esc(a.drift.join("、")) + "（手工改过 holdings.json？下一笔流水保存时会以流水为准回写）</div>";
    html += '<div class="sec-title">逐只归因（人民币）</div><div class="p1-tbl-wrap"><table class="p1-tbl"><thead><tr><th>标的</th><th>持股</th><th>成本</th><th>现价</th><th>盈亏</th><th>市场</th><th>选股</th><th>时机</th><th>汇率</th><th>基准</th></tr></thead><tbody>' +
      a.rows.map(function (r) {
        return "<tr" + (r.held ? "" : ' class="p1-dim"') + "><td><b>" + esc(r.name) + '</b><div class="p1-sub">' + esc(r.code) + " · " + r.trades + " 笔</div></td><td>" + W().pv(n2(r.shares, 0)) + "</td><td>" + W().pv(n2(r.cost)) + "</td><td>" + n2(r.price) + "</td>" +
          ['pnl_cny', 'market_cny', 'selection_cny', 'timing_cny', 'fx_cny'].map(function (k) { return '<td class="' + cls(r[k]) + '">' + yuan(r[k]) + (k === "pnl_cny" && r.pnl_pct != null ? '<div class="p1-sub">' + sg(r.pnl_pct) + "%</div>" : "") + "</td>"; }).join("") +
          '<td class="p1-sub">' + esc(r.bench || "") + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    html += '<div class="p1-legend" style="line-height:1.8">市场：同样这笔钱买大盘指数（港股看恒生指数、美股看标普 500）到今天的涨跌；时机：成交价 vs 成交日前后 10 个交易日均价，买得比均价低为正；汇率：港币/美元折人民币的变化；选股：总盈亏减去前三项的差额，反映个股自身相对大盘的表现。四项严格加总 = 浮动盈亏；已卖出落袋的部分在「已实现 / 分红」里，不参与拆分。行情与汇率取成交日收盘，误差约一个交易日。</div>';
    html += '<div class="p1-legend">' + esc(d.note) + "</div>";
    html += '<div class="sec-title">记一笔</div>' + tradeForm(d);
    html += '<div class="sec-title">流水 · ' + d.items.length + " 笔</div>" + '<div class="p1-tlist">' + d.items.map(tradeRow).join("") + "</div>";
    return html;
  };
  function tradeForm(d) {
    var opts = W().allStocks().map(function (s) { return '<option value="' + esc(s.code) + '">' + esc(s.name) + " " + esc(s.code) + "</option>"; }).join("");
    var today = new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10);
    return '<div class="pl-form p1-tform" id="trForm"><div class="frow">' +
      '<select id="trSide"><option value="buy">买入</option><option value="sell">卖出</option><option value="dividend">分红</option><option value="split">拆/合股</option></select>' +
      '<select id="trCode">' + opts + "</select>" + '<input type="date" id="trDate" value="' + today + '" max="' + today + '"></div>' +
      '<div class="frow" data-tr-for="buy sell"><input type="number" id="trPrice" step="any" min="0" placeholder="成交价（本币）"><input type="number" id="trShares" step="any" min="0" placeholder="股数"><input type="number" id="trFee" step="any" min="0" placeholder="费用（可选）"></div>' +
      '<div class="frow" data-tr-for="dividend" hidden><input type="number" id="trAmount" step="any" min="0" placeholder="分红现金总额（本币，税后）"></div>' +
      '<div class="frow" data-tr-for="split" hidden><input type="number" id="trRatio" step="any" min="0" placeholder="拆股比例：1 拆 N 填 N，N 合 1 填 1/N 的小数"></div>' +
      '<div class="frow" data-tr-for="sell" hidden><select id="trCat"><option value="">— 卖出原因类别（必选）—</option>' + d.sell_reasons.map(function (x) { return "<option>" + esc(x) + "</option>"; }).join("") + "</select></div>" +
      '<div class="frow"><textarea id="trReason" maxlength="1000" placeholder="理由：为什么现在做这笔（卖出必填 ≥10 字；会自动关联该股逻辑卡存档）"></textarea></div>' +
      '<div id="trDisc"></div>' +
      '<div class="fbar"><span class="nt-hint">买入/卖出保存前自动做纪律检查；成本与股数将按流水回写持仓</span><div class="right"><button class="nt-add" id="trAdd">保存流水</button></div></div></div>';
  }
  function tradeRow(t) {
    var main = t.side === "dividend" ? "现金 " + W().pv(n2(t.amount)) : t.side === "split" ? "比例 ×" + t.ratio : n2(t.price) + " × " + W().pv(n2(t.shares, 0));
    return '<div class="p1-trow"><span class="p1-side s-' + t.side + '">' + (SIDE_TXT[t.side] || t.side) + "</span>" +
      (t.side === "init" ? '<input type="date" class="p1-idate" data-tr-date="' + esc(t.id) + '" value="' + esc(t.date) + '" title="改成实际建仓日，市场/选股拆分更准确">' : '<span class="p1-date">' + esc(t.date) + "</span>") +
      "<b>" + esc(t.name) + '</b><span class="p1-num">' + main + (t.fee ? " · 费 " + n2(t.fee) : "") + "</span>" +
      (t.reason_cat ? '<span class="p1-badge p1-muted-b">' + esc(t.reason_cat) + "</span>" : "") +
      (t.discipline ? P1.lvBadge(t.discipline.level) : "") +
      (t.thesis_ref ? '<span class="p1-badge p1-muted-b" title="' + esc(t.thesis_ref.summary || "") + '">已关联逻辑卡</span>' : "") +
      '<span class="p1-reason-t">' + esc(t.side === "init" ? "" : t.reason || "") + "</span>" +
      (t.side !== "init" ? '<button class="nt-ghost p1-mini" data-tr-del="' + esc(t.id) + '">删除</button>' : "") + "</div>";
  }
  function trSync() {
    var side = byId("trSide").value;
    Array.prototype.forEach.call(document.querySelectorAll("[data-tr-for]"), function (el) { el.hidden = el.getAttribute("data-tr-for").split(" ").indexOf(side) < 0; });
    byId("trDisc").innerHTML = "";
  }
  function trApply(r, msg) {
    if (r && r.ok) { S().trades = r; W().toast(msg); W().refreshHoldings && W().refreshHoldings(); W().renderMain(); return true; }
    return false;
  }
  P1.bindTrades = function () {
    if (!byId("trForm")) return;
    byId("trSide").addEventListener("change", trSync);
    byId("trCode").addEventListener("change", function () { byId("trDisc").innerHTML = ""; });
    byId("trAdd").addEventListener("click", function () {
      var side = byId("trSide").value, btn = byId("trAdd");
      var body = { op: "add", side: side, code: byId("trCode").value, date: byId("trDate").value, price: byId("trPrice").value, shares: byId("trShares").value, fee: byId("trFee").value,
        amount: byId("trAmount").value, ratio: byId("trRatio").value, reason_cat: byId("trCat").value, reason: byId("trReason").value.trim(),
        discipline_reason: byId("trDiscReason") ? byId("trDiscReason").value.trim() : "" };
      btn.disabled = true;
      W().apiPost("/api/trades", body).then(function (r) {
        btn.disabled = false;
        if (trApply(r, "已记录流水，持仓成本已更新")) return;
        if (r.error === "discipline_ack_required") {
          var keep = byId("trDiscReason") ? byId("trDiscReason").value : "";
          byId("trDisc").innerHTML = P1.renderDisc(r.check, "trDiscReason");
          if (byId("trDiscReason")) { byId("trDiscReason").value = keep; byId("trDiscReason").focus(); }
        }
        W().toast(r.message || ("保存失败：" + (r.error || "网络异常")));
      });
    });
    on("[data-tr-del]", "click", function (el) {
      if (!confirm("删除这笔流水？持仓成本会按剩余流水重算。")) return;
      W().apiPost("/api/trades", { op: "delete", id: el.getAttribute("data-tr-del") }).then(function (r) { if (!trApply(r, "已删除")) W().toast("删除失败：" + r.error); });
    });
    on("[data-tr-date]", "change", function (el) {
      W().apiPost("/api/trades", { op: "update", id: el.getAttribute("data-tr-date"), date: el.value }).then(function (r) { if (!trApply(r, "期初日期已更新")) W().toast("更新失败：" + r.error); });
    });
  };

  /* ================= P1-3 信号准确率看板 ================= */
  P1.loadAcc = function (force) { load("acc", "/api/accuracy" + (force ? "?force=1" : ""), null, force); };
  P1.accMult = function (pid) { var d = S().acc; return (d && d.persona_mult && d.persona_mult[pid]) || 1; };
  function pct(v) { return v == null ? "—" : n2(v, 1) + "%"; }
  function rateCls(v) { return v == null ? "" : v >= 53 ? "up" : v <= 47 ? "down" : ""; }
  /* 有效样本量修正（2026-10-16 用户确认）：逐日样本持有期重叠，独立样本约 n/h；
     「有效/反向」需 ≥30 个独立样本且偏离 50% 达到统计显著（二项 95% 区间），否则「证据不足」 */
  function verdict(x, h) {
    if (!x || !x.ex_n) return '<span class="p1-badge p1-muted-b">无样本</span>';
    var nEff = x.ex_n / (h || 60);
    if (nEff < 30) return '<span class="p1-badge p1-muted-b" title="独立样本约 ' + Math.round(nEff) + " 个（逐日重叠已折算），不足 30 不下结论\">证据不足</span>";
    var sig = 1.96 * Math.sqrt(0.25 / nEff) * 100;
    if (x.ex_rate - 50 > sig && x.ex_edge > 0) return '<span class="p1-badge p1-red">有效</span>';
    if (50 - x.ex_rate > sig && x.ex_edge < 0) return '<span class="p1-badge p1-ok">反向</span>';
    return '<span class="p1-badge p1-muted-b">接近随机</span>';
  }
  function verdictText(x, h) {  /* 简表用纯文本结论 */
    var t = verdict(x, h);
    return t.indexOf("有效") >= 0 && t.indexOf("证据") < 0 ? "有效" : t.indexOf("反向") >= 0 ? "反向" : t.indexOf("证据不足") >= 0 ? "证据不足" : t.indexOf("无样本") >= 0 ? "无样本" : "接近随机";
  }
  function hCells(x) {
    return '<td class="' + rateCls(x.rate) + '">' + pct(x.rate) + '</td><td class="' + rateCls(x.ex_rate) + '">' + pct(x.ex_rate) + '</td><td class="' + cls(x.ex_edge) + '">' + sg(x.ex_edge, 2) + '%<div class="p1-sub">n=' + (x.ex_n || 0) + "</div></td>";
  }
  function monthChart(ms) {
    ms = ms.filter(function (m) { return m.ex_n >= 20; }).slice(-18);
    if (ms.length < 3) return "";
    var W0 = 640, H0 = 150, pad = 26, bw = (W0 - pad * 2) / ms.length, y = function (v) { return H0 - 22 - ((v - 20) / 60) * (H0 - 36); };
    var bars = ms.map(function (m, i) {
      var v = Math.max(20, Math.min(80, m.ex_rate)), x = pad + i * bw + bw * 0.18, y0 = y(50), y1 = y(v);
      var c = v >= 50 ? "var(--up)" : "var(--down)";
      return '<rect x="' + x.toFixed(1) + '" y="' + Math.min(y0, y1).toFixed(1) + '" width="' + (bw * 0.64).toFixed(1) + '" height="' + Math.max(1, Math.abs(y1 - y0)).toFixed(1) + '" rx="2" fill="' + c + '" opacity="0.8"><title>' + esc(m.month + " 超额命中 " + pct(m.ex_rate) + " · n=" + m.ex_n) + "</title></rect>" +
        (i % 3 === 0 || i === ms.length - 1 ? '<text x="' + (x + bw * 0.32).toFixed(1) + '" y="' + (H0 - 6) + '" font-size="10" text-anchor="middle" fill="var(--ink-3)">' + m.month.slice(2) + "</text>" : "");
    }).join("");
    var grid = [30, 50, 70].map(function (g) { return '<line x1="' + pad + '" x2="' + (W0 - pad) + '" y1="' + y(g).toFixed(1) + '" y2="' + y(g).toFixed(1) + '" stroke="var(--line)"' + (g === 50 ? ' stroke-width="1.4"' : ' stroke-dasharray="3 3"') + '/><text x="' + (pad - 4) + '" y="' + (y(g) + 3).toFixed(1) + '" font-size="10" text-anchor="end" fill="var(--ink-3)">' + g + "</text>"; }).join("");
    return '<svg class="p1-mchart" viewBox="0 0 ' + W0 + " " + H0 + '" width="100%" preserveAspectRatio="xMidYMid meet">' + grid + bars + "</svg>";
  }
  P1.renderAccuracy = function () {
    var d = S().acc;
    if (!d || d.loading) { P1.loadAcc(false); return '<div class="empty">准确率计算中…（需拉取全部标的与基准的日 K，首次约 5-10 秒）</div>'; }
    if (!d.ok) return '<div class="empty">准确率不可用：' + esc(d.error) + "</div>";
    var b = d.backtest, lv = d.live, H = d.horizons, c60 = b.combo.h60, nEff = Math.round(c60.ex_n / 60);
    var sig = 1.96 * Math.sqrt(0.25 / nEff) * 100;
    var edge = c60.ex_rate - 50;
    var concl = edge > sig && c60.ex_edge > 0 ? ["有效", "s"] : (50 - c60.ex_rate > sig && c60.ex_edge < 0 ? ["反向", "w"] : ["弱有效", "m"]);
    /* ① 一句话结论卡 */
    var html = '<div class="acc-hero"><div class="acc-k">这套系统的信号准不准</div>' +
      '<div class="acc-v ' + concl[1] + '">' + concl[0] + "</div>" +
      '<div class="acc-t">价格信号在 60 天视角跑赢大盘的比例是 <b>' + c60.ex_rate + '%</b>（约 ' + nEff + ' 个独立区间）' +
      (concl[0] === "有效" ? "，有稳定优势" : concl[0] === "反向" ? "，方向反了" : "，只比抛硬币好一点，优势不明显") + "。</div>" +
      '<div class="acc-a">建议：价格只用来看「现在处在什么位置」，不用来预测涨跌——决策页也是这么做的。</div>' +
      '<div class="acc-books">回测 <b>已完成</b> · 信号账本 <b>记账中</b>（已记 ' + lv.entries + " 条，首批约 " + (lv.first_mature_in == null ? "—" : lv.first_mature_in) + " 个交易日后有结果）· 大师账本 <b>记账中</b>（" + d.expert_ledger.entries + " 条观点）</div></div>";
    /* ② 因子简表（60 日口径，程序拼一句话） */
    var rows = [{ name: "合成信号（3 因子）", x: b.combo, strong: true }].concat(b.factors.map(function (f) { return { name: f.name, x: f }; }));
    html += '<div class="sec-title">每个因子有没有用 <span style="font-weight:400;font-size:0.74rem;color:var(--ink-3)">60 天视角 · 按有效样本量判定</span></div>' +
      '<div class="p1-card" style="padding:2px 16px">' + rows.map(function (r) {
        var x = r.x.h60, v = verdictText(x, 60), ne = x && x.ex_n ? Math.round(x.ex_n / 60) : 0;
        var cls2 = v === "有效" ? "s" : v === "反向" ? "w" : v === "接近随机" ? "m" : "n";
        var line = !x || !x.ex_n ? "没有可回测的样本" :
          v === "证据不足" ? "独立样本约 " + ne + " 个，太少，暂不下结论" :
          "60 天后跑赢大盘的比例 " + x.ex_rate + "%（约 " + ne + " 个独立区间）" +
          (v === "有效" ? "，有优势" : v === "反向" ? "，方向反了，应降权或反用" : "，接近随机");
        return '<div class="acc-row' + (r.strong ? " strong" : "") + '"><span class="n">' + esc(r.name) + "</span>" +
          '<span class="p1-badge ' + (cls2 === "s" ? "p1-red" : cls2 === "w" ? "p1-ok" : "p1-muted-b") + '">' + v + "</span>" +
          '<span class="t">' + line + "</span></div>";
      }).join("") + "</div>";
    /* ③ 月度稳定性图（最直观，保留直显） */
    var mc = monthChart(b.monthly || []);
    if (mc) html += '<div class="sec-title">月度稳定性 · 合成信号 20 日超额命中率</div><div class="p1-card">' + mc + '<div class="p1-legend" style="margin:4px 0 0">红柱 = 当月跑赢随机（50% 线以上），绿柱 = 跑输。连续多月绿色说明当前市场风格下价格因子失效。</div></div>';
    /* ④ 账本状态：未成熟只一行，成熟才有表 */
    var led = [];
    led.push(lv.tot.h20.n ? null : "信号账本：自 " + esc(lv.since || "—") + " 起已记 " + lv.entries + " 条（有方向 " + lv.directional + " 条），20 日后的成绩单还没到期，首批约 " + (lv.first_mature_in == null ? "—" : lv.first_mature_in) + " 个交易日后出来。");
    var pendingAll = d.experts.every(function (e) { return !e.basis; });
    if (pendingAll) {
      var eta0 = Math.max.apply(null, d.experts.map(function (e) { return e.pending ? 0 : 999; }).concat([0]));
      var eta = d.experts.filter(function (e) { return e.eta != null; }).map(function (e) { return e.eta; });
      led.push("大师账本：已记 " + d.expert_ledger.entries + " 条观点，首批约 " + (eta.length ? Math.min.apply(null, eta) : "—") + " 个交易日后到期；在那之前所有大师的权重系数都是 ×1.00（谁也不多信一分）。");
    }
    if (led.length) html += '<div class="p1-card p1-muted">' + led.filter(Boolean).join("<br>") + "</div>";
    /* ⑤ 明细与口径（默认收起）：三周期完整表 + 按市场 + 逐只 + 账本 */
    var det = '<div class="sec-title" style="margin-top:4px">价格因子回测 · 按因子拆分</div><div class="p1-tbl-wrap"><table class="p1-tbl"><thead><tr><th>信号</th><th>判定</th>' +
      H.map(function (h) { return '<th colspan="3" class="p1-hgrp">' + h + " 日</th>"; }).join("") + "</tr><tr><th></th><th></th>" +
      H.map(function () { return "<th>命中</th><th>超额命中</th><th>方向超额</th>"; }).join("") + "</tr></thead><tbody>" +
      rows.map(function (r) {
        return "<tr><td>" + (r.strong ? "<b>" + esc(r.name) + "</b>" : esc(r.name)) + "</td><td>" + verdict(r.x.h60, 60) + "</td>" + H.map(function (h) { return hCells(r.x["h" + h]); }).join("") + "</tr>";
      }).join("") +
      '<tr class="p1-dim"><td>基础概率（一直看多）</td><td></td>' + H.map(function (h) { var bs = b.base["h" + h]; return "<td>" + pct(bs.up_rate) + "</td><td>" + pct(bs.ex_up_rate) + '</td><td class="' + cls(bs.ex_avg) + '">' + sg(bs.ex_avg, 2) + "%</td>"; }).join("") + "</tr>" +
      "</tbody></table></div>";
    det += '<div class="p1-legend">判定按 60 日超额口径，已按有效样本量修正：逐日样本持有期重叠，独立样本约 n÷周期天数；需 ≥30 个独立样本且偏离 50% 达到统计显著（二项 95% 区间）才记「有效/反向」，否则「接近随机/证据不足」。' + esc(b.note) + "</div>";
    det += '<div class="sec-title">按市场 · 合成信号</div><div class="p1-tbl-wrap"><table class="p1-tbl"><thead><tr><th>市场（基准）</th>' + H.map(function (h) { return '<th colspan="3" class="p1-hgrp">' + h + " 日</th>"; }).join("") + "</tr></thead><tbody>" +
      b.markets.map(function (m) { return "<tr><td>" + esc(m.bench) + "</td>" + H.map(function (h) { return hCells(m["h" + h]); }).join("") + "</tr>"; }).join("") + "</tbody></table></div>";
    det += '<div class="sec-title">逐只 · 合成信号 60 / 120 日</div><div class="p1-tbl-wrap"><table class="p1-tbl"><thead><tr><th>标的</th><th>样本日</th><th>60 日命中</th><th>60 日超额命中</th><th>60 日基础上涨率</th><th>120 日超额命中</th><th>120 日方向超额</th></tr></thead><tbody>' +
      b.stocks.map(function (s) {
        if (s.error) return '<tr class="p1-dim"><td><b>' + esc(s.name) + '</b><div class="p1-sub">' + esc(s.code) + '</div></td><td colspan="6" style="text-align:left">' + esc(s.error) + "（上市时间短）</td></tr>";
        return "<tr" + (s.held ? "" : ' class="p1-dim"') + "><td><b>" + esc(s.name) + '</b><div class="p1-sub">' + esc(s.code) + (s.held ? "" : " · 关注") + "</div></td><td>" + s.samples + '</td><td class="' + rateCls(s.h60.rate) + '">' + pct(s.h60.rate) + '</td><td class="' + rateCls(s.h60.ex_rate) + '">' + pct(s.h60.ex_rate) + "</td><td>" + pct(s.base60.up_rate) + '</td><td class="' + rateCls(s.h120.ex_rate) + '">' + pct(s.h120.ex_rate) + '</td><td class="' + cls(s.h120.ex_edge) + '">' + sg(s.h120.ex_edge, 2) + "%</td></tr>";
      }).join("") + "</tbody></table></div>";
    det += '<div class="sec-title">信号账本 · 8 因子完整信号（实盘记账）</div>';
    if (!lv.tot.h20.n) det += '<div class="p1-card p1-muted">尚无成熟样本。每次打开决策中心/信号接口都会补记当天。</div>';
    else det += '<div class="p1-tbl-wrap"><table class="p1-tbl"><thead><tr><th>置信度</th>' + H.map(function (h) { return '<th colspan="3" class="p1-hgrp">' + h + " 日</th>"; }).join("") + "</tr></thead><tbody>" +
      [{ level: "全部", x: lv.tot }].concat(lv.by_conf.map(function (c) { return { level: c.level, x: c }; })).map(function (r) { return "<tr><td>" + esc(r.level) + "</td>" + H.map(function (h) { return hCells(r.x["h" + h]); }).join("") + "</tr>"; }).join("") + "</tbody></table></div>";
    det += '<div class="sec-title">大师账本 · 按人拆分（准确率反向调节大师共识权重）</div>';
    if (pendingAll) det += '<div class="p1-card p1-muted">观点在记账，首批还没到期；到期后按 20 / 60 / 120 日超额命中拆分，胜率高的权重系数上调（最高 ×1.5），低的下调（最低 ×0.5）。</div>';
    else det += '<div class="p1-tbl-wrap"><table class="p1-tbl"><thead><tr><th>大师</th><th>观点</th><th>多 / 空</th><th>20 日超额命中</th><th>60 日超额命中</th><th>120 日超额命中</th><th>共识权重系数</th><th>状态</th></tr></thead><tbody>' +
      d.experts.map(function (e) {
        var st = e.basis ? "按 " + e.basis.slice(1) + " 日口径生效" : e.pending ? "待成熟 " + e.pending + " 条 · 约 " + (e.eta == null ? "—" : e.eta) + " 个交易日后首批到期" : "还需 " + e.need + " 条成熟样本";
        var cell = function (x) { return '<td class="' + rateCls(x.ex_rate) + '">' + pct(x.ex_rate) + '<div class="p1-sub">n=' + x.ex_n + "</div></td>"; };
        return "<tr><td><b>" + esc(e.name) + "</b></td><td>" + e.opinions + "</td><td>" + e.bull + " / " + e.bear + "</td>" + cell(e.h20) + cell(e.h60) + cell(e.h120) +
          '<td class="' + (e.mult > 1 ? "up" : e.mult < 1 ? "down" : "") + '"><b>×' + n2(e.mult) + "</b></td>" + '<td style="text-align:left" class="p1-sub">' + esc(st) + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    det += '<div class="p1-legend">' + esc(d.rule) + "。样本不足时系数恒为 ×1.00，不会因少数几次猜对/猜错大幅改变权重。</div>";
    html += '<details class="dc-fold" data-fold="acc-det"><summary>明细与口径<span class="sub">三周期完整表 · 按市场 · 逐只 · 两个账本</span></summary><div style="padding:4px 16px 10px">' + det + "</div></details>";
    html += '<div class="fbar"><span class="nt-hint">计算于 ' + esc(d.at) + "（缓存 30 分钟）</span><div class=\"right\"><button class=\"nt-ghost\" id=\"accRefresh\">重新计算</button></div></div>";
    return html;
  };
  P1.bindAccuracy = function () {
    var b = byId("accRefresh"); if (!b) return;
    b.addEventListener("click", function () { b.disabled = true; P1.loadAcc(true); W().renderMain(); });
  };

  /* ================= P1-7 动态估值 ================= */
  P1.loadVal = function (code, force) {
    load("val_" + code, "/api/valuation?code=" + encodeURIComponent(code) + (force ? "&force=1" : ""), null, force);
  };
  var MLAB = { pe: "PE", pb: "PB", ps: "PS" };
  function capFmt(v) {
    if (v == null || !isFinite(v)) return "—";
    var a = Math.abs(v);
    return (v < 0 ? "−" : "") + (a >= 1e12 ? (a / 1e12).toFixed(2) + " 万亿" : a >= 1e8 ? (a / 1e8).toFixed(1) + " 亿" : a >= 1e4 ? (a / 1e4).toFixed(1) + " 万" : a.toFixed(0));
  }
  function mosBadge(m) {
    if (m == null || !isFinite(m)) return "—";
    var c = m >= 0.3 ? "va-good" : m >= 0 ? "p1-amber" : "va-bad";
    return '<span class="p1-badge ' + c + '">' + (m >= 0 ? "" : "−") + Math.abs(m * 100).toFixed(0) + "%</span>";
  }
  function zoneCls(r) { return r == null ? "" : r <= 20 ? "p1-vz-lo" : r >= 80 ? "p1-vz-hi" : ""; }
  function bandChart(series, key, mt) {
    var pts = series.filter(function (x) { return x[key] != null && x[key] > 0; });
    if (pts.length < 10 || !mt.band.p50) return '<div class="p1-muted">样本不足，无法绘制分位带</div>';
    var W0 = 680, H0 = 220, L = 40, R = 60, T = 12, B = 24;
    var vs = pts.map(function (x) { return x[key]; }).concat([mt.cur || mt.band.p50]);
    var lo = Math.min.apply(null, vs), hi = Math.max.apply(null, vs), sp = (hi - lo) || 1; lo -= sp * 0.06; hi += sp * 0.06; sp = hi - lo;
    var X = function (i) { return L + i / (pts.length - 1) * (W0 - L - R); };
    var Y = function (v) { return T + (1 - (v - lo) / sp) * (H0 - T - B); };
    var bd = mt.band, h = "";
    h += '<rect x="' + L + '" y="' + Y(bd.p75).toFixed(1) + '" width="' + (W0 - L - R) + '" height="' + Math.max(1, Y(bd.p25) - Y(bd.p75)).toFixed(1) + '" fill="var(--accent)" opacity="0.10"/>';
    [["p10", "3 3"], ["p25", ""], ["p50", ""], ["p75", ""], ["p90", "3 3"]].forEach(function (b) {
      var v = bd[b[0]]; if (v == null) return;
      h += '<line x1="' + L + '" x2="' + (W0 - R) + '" y1="' + Y(v).toFixed(1) + '" y2="' + Y(v).toFixed(1) + '" stroke="' + (b[0] === "p50" ? "var(--ink-3)" : "var(--line)") + '" stroke-width="' + (b[0] === "p50" ? 1.3 : 1) + '"' + (b[1] ? ' stroke-dasharray="' + b[1] + '"' : "") + "/>" +
        '<text x="' + (W0 - R + 6) + '" y="' + (Y(v) + 3.5).toFixed(1) + '" font-size="10.5" fill="var(--ink-3)">' + b[0].toUpperCase() + " " + n2(v, 1) + "</text>";
    });
    var path = pts.map(function (p, i) { return (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(p[key]).toFixed(1); }).join(" ");
    h += '<path d="' + path + '" fill="none" stroke="var(--ink)" stroke-width="1.5"/>';
    var yr = "";
    pts.forEach(function (p, i) {
      var y = p.d.slice(0, 4);
      if (y !== yr) { yr = y; if (i > 0) h += '<text x="' + X(i).toFixed(1) + '" y="' + (H0 - 6) + '" font-size="10.5" text-anchor="middle" fill="var(--ink-3)">' + y + "</text>"; }
    });
    [lo + sp * 0.1, lo + sp * 0.9].forEach(function (v) { h += '<text x="' + (L - 6) + '" y="' + (Y(v) + 3).toFixed(1) + '" font-size="10" text-anchor="end" fill="var(--ink-3)">' + n2(v, 1) + "</text>"; });
    if (mt.cur) h += '<circle cx="' + X(pts.length - 1).toFixed(1) + '" cy="' + Y(mt.cur).toFixed(1) + '" r="4" fill="var(--accent)"><title>当前 ' + n2(mt.cur, 2) + "</title></circle>";
    return '<svg class="p1-vchart" viewBox="0 0 ' + W0 + " " + H0 + '" width="100%" preserveAspectRatio="xMidYMid meet">' + h + "</svg>";
  }
  /* ---- 估值 Tab v2（2026-10-15 用户确认）：结论卡 + 可信度 + 三把尺子大白话；技术表收进「怎么算的」 ----
     配色：红 = 便宜（好）、绿 = 偏贵（差），与全站红绿约定一致。大白话由程序按模板从原始数字拼，不交给模型。 */
  var VA_TONE = { "明显便宜": "good", "偏便宜": "good", "合理": "mid", "偏贵": "bad", "明显偏贵": "bad" };
  function vaTag(s) { return s == null ? '<span class="va-t na">不适用</span>' : s > 0 ? '<span class="va-t good">便宜</span>' : s < 0 ? '<span class="va-t bad">偏贵</span>' : '<span class="va-t mid">合理</span>'; }
  function vaRows(d) {
    var R = d.rulers || {}, m = d.metrics[d.primary] || {}, dcf = d.dcf || {}, pe = d.peers || {};
    var rows = [];
    /* ① 和自己比 */
    var h = R.hist || {};
    rows.push(["和自己比", h.s, h.s == null ? h.na :
      (m.rank <= 50 ? "比过去 5 年 " + (100 - m.rank) + "% 的时候都便宜" : "比过去 5 年 " + m.rank + "% 的时候都贵") + (d.high_growth ? "，但营收高增长会让历史倍数被动变低，已降权" : ""),
      h.s == null ? "" : MLAB[d.primary] + " " + n2(m.cur, 1) + " 倍，5 年中位 " + n2(m.band.p50, 1) + " 倍" + (d.primary === "ps" ? "；市盈率历史不全，改看市销率" : "")]);
    /* ② 和增长比 */
    var c = R.dcf || {};
    rows.push(["和增长比", c.s, c.s == null ? c.na :
      "现价只隐含每年 " + n2(dcf.implied_g * 100, 1) + "% 的增长，" + (dcf.mos >= 0 ? "估出的内在价值比现价高 " + Math.round(dcf.mos * 100) + "%" : "估出的内在价值比现价低 " + Math.round(-dcf.mos * 100) + "%"),
      c.s == null ? "" : "反向 DCF" + (dcf.rev_cagr != null ? "；过去 " + dcf.rev_cagr_years + " 年营收年增 " + n2(dcf.rev_cagr * 100, 1) + "%" : "")]);
    /* ③ 和同行比 */
    var p = R.peer || {}, pl = (pe.list || []).filter(function (x) { return !x.missing && x.pe > 0; }), self = pl.filter(function (x) { return x.self; })[0];
    var others = pl.filter(function (x) { return !x.self; }), nCh = self ? others.filter(function (x) { return x.pe > self.pe; }).length : 0;
    rows.push(["和同行比", p.s, p.s == null ? p.na :
      "比 " + others.length + " 家同行里 " + nCh + " 家便宜，" + (pe.prem_pe < 0 ? "比同行中位低 " : "比同行中位高 ") + Math.round(Math.abs(pe.prem_pe) * 100) + "%",
      p.s == null ? "" : "市盈率 " + n2(self.pe, 1) + " 倍，同行中位 " + n2(pe.median.pe, 1) + " 倍"]);
    return rows;
  }
  function vaPlain(d, rows) {
    var ok = rows.filter(function (r) { return r[1] != null; });
    if (!ok.length) return "三把尺子都用不了，暂时判断不了贵不贵。";
    var g = ok.filter(function (r) { return r[1] > 0; }).length, b = ok.filter(function (r) { return r[1] < 0; }).length;
    var head = ok.length === 3 ? "三把尺子" : ok.length + " 把能用的尺子";
    if (g === ok.length) return head + (ok.length > 1 ? "都" : "") + "说不贵。";
    if (b === ok.length) return head + (ok.length > 1 ? "都" : "") + "说偏贵。";
    return head + "看法不一：" + ok.map(function (r) { return r[0] + (r[1] > 0 ? "便宜" : r[1] < 0 ? "偏贵" : "合理"); }).join("、") + "。";
  }
  P1.renderValuation = function (code) {
    var d = S()["val_" + code];
    if (!d || d.loading) { P1.loadVal(code, false); return '<div class="empty">估值读取中…</div>'; }
    if (!d.ok) return '<div class="empty">暂时算不出估值：' + esc(d.reason || d.error) + "</div>";
    var M = d.metrics, pm = d.primary, dcf = d.dcf, pe = d.peers;
    var sel = (S().valM || {})[code] || pm;
    var rows = vaRows(d), vd = d.verdict || d.overall, tone = VA_TONE[vd] || "mid";
    var when = String(d.precomputed_at || d.at || "").slice(5, 16).replace("T", " ");
    /* ① 结论卡 */
    var html = '<div class="va-card"><div class="va-k">贵不贵</div>' +
      '<div class="va-hd"><span class="va-v ' + tone + '">' + esc(vd) + "</span>" +
      '<span class="va-cf ' + (d.confidence === "低" ? "lo" : "") + '">' + (d.rulers_ok || 0) + " 把尺子能用 · 可信度" + esc(d.confidence || "--") + "</span></div>" +
      '<div class="va-plain">' + esc(vaPlain(d, rows)) + (d.confidence === "低" && d.rulers_ok ? "能用的依据太少，结论最多到「偏」，仅供参考。" : "") + "</div>" +
      '<div class="va-rows">' + rows.map(function (r) {
        return '<div class="va-r"><span class="n">' + r[0] + "</span>" + vaTag(r[1]) + '<span class="t' + (r[1] == null ? " na" : "") + '">' + esc(r[2] || "") +
          (r[3] ? ' <span class="g">（' + esc(r[3]) + "）</span>" : "") + "</span></div>";
      }).join("") + "</div>" +
      '<div class="va-ft">计算于 ' + esc(when || "--") + (d.stale ? " · 本次更新失败，显示上一次结果" : "") + " · 每天 06:30 自动更新</div></div>";
    /* ② 5 年估值走势 */
    html += '<div class="va-card"><div class="va-k">5 年估值走势</div><div class="p1-vtabs" style="margin-top:8px">' + ["pe", "pb", "ps"].map(function (k) {
      return '<button class="nt-ghost' + (k === sel ? " active" : "") + '" data-valm="' + k + '" data-code="' + esc(code) + '"' + (M[k].n ? "" : " disabled") + ">" + MLAB[k] + (k === pm ? " · 主看" : "") + "</button>";
    }).join("") + "</div>" + bandChart(d.series, sel, M[sel]) +
      '<div class="p1-legend" style="margin:4px 0 0">浅色带是过去 5 年的常态区间，灰线是中位，圆点是现在。</div></div>';
    /* ③ 怎么算的（默认收起） */
    var how = "";
    how += '<div class="va-h">历史分位</div><div class="p1-tbl-wrap"><table class="p1-tbl"><thead><tr><th>倍数</th><th>当前</th><th>5 年分位</th><th>P10</th><th>P25</th><th>P50</th><th>P75</th><th>P90</th><th>样本周</th></tr></thead><tbody>' +
      ["pe", "pb", "ps"].map(function (k) {
        var m = M[k], b = m.band;
        return "<tr" + (k === pm ? "" : ' class="p1-dim"') + "><td><b>" + MLAB[k] + "</b>" + (k === pm ? ' <span class="p1-sub">主</span>' : "") + "</td><td>" + n2(m.cur, 2) + '</td><td class="' + zoneCls(m.rank) + '">' + (m.rank == null ? "—" : m.rank + "%") + "</td><td>" + n2(b.p10, 1) + "</td><td>" + n2(b.p25, 1) + "</td><td>" + n2(b.p50, 1) + "</td><td>" + n2(b.p75, 1) + "</td><td>" + n2(b.p90, 1) + "</td><td>" + m.n + "</td></tr>";
      }).join("") + "</tbody></table></div>";
    var pb = d.price_band;
    if (pb && pb.p50) how += '<div class="p1-legend">按 ' + MLAB[pm] + " 换算的价格（" + esc(d.currency) + "）：P10 " + n2(pb.p10, 2) + " · P25 " + n2(pb.p25, 2) + " · P50 " + n2(pb.p50, 2) + " · P75 " + n2(pb.p75, 2) + " · P90 " + n2(pb.p90, 2) + "；现价 " + n2(d.price, 2) + "。分位 ≤20% 记 +2、≤40% +1、≥60% −1、≥80% −2" + (d.high_growth ? "；高成长股封顶 ±1" : "") + "。</div>";
    how += '<div class="va-h">反向 DCF</div>';
    if (!dcf.ok) how += '<div class="p1-legend">' + esc(dcf.reason || "不适用") + "</div>";
    else {
      how += '<div class="p1-tbl-wrap"><table class="p1-tbl"><thead><tr><th>情景</th><th>1–5 年增速</th><th>6–10 年</th><th>内在价值 / 股</th><th>安全边际</th></tr></thead><tbody>' +
        dcf.scenarios.map(function (s) {
          return "<tr" + (s.name === "基准" ? "" : ' class="p1-dim"') + "><td><b>" + esc(s.name) + "</b></td><td>" + n2(s.g1 * 100, 1) + "%</td><td>线性降至 " + n2(dcf.term_g * 100, 0) + "%</td><td>" + n2(s.iv, 2) + " " + esc(d.currency) + "</td><td>" + mosBadge(s.mos) + "</td></tr>";
        }).join("") + "</tbody></table></div>" +
        '<div class="p1-legend">TTM 自由现金流 ' + capFmt(dcf.fcf_ttm) + " " + esc(d.stmt_currency || "") + "（" + esc(dcf.fcf_end) + "）· FCF 收益率 " + n2(dcf.fcf_yield * 100, 1) + "% · 隐含 10 年增速 " + n2(dcf.implied_g * 100, 1) + "%" + (dcf.implied_bound ? "（触及边界）" : "") +
        " · WACC " + n2(dcf.wacc * 100, 1) + "% / 永续 " + n2(dcf.term_g * 100, 1) + "%。WACC 敏感性：" + dcf.sens.map(function (s) { return n2(s.wacc * 100, 1) + "% → " + n2(s.iv, 2); }).join(" · ") +
        "。基准增速 = 过去营收年增速截断到 [−5%, 15%]。安全边际 ≥30% 记 +2、≥0 +1、<0 −1、<−30% −2。</div>";
    }
    how += '<div class="va-h">可比公司</div>';
    if (!pe.configured) how += '<div class="p1-legend">还没有配置可比公司。</div>';
    else how += '<div class="p1-tbl-wrap"><table class="p1-tbl"><thead><tr><th>公司</th><th>PE(TTM)</th><th>PB</th><th>股息率</th><th>总市值（亿）</th></tr></thead><tbody>' +
      pe.list.map(function (x) {
        if (x.missing) return '<tr class="p1-dim"><td>' + esc(x.ws) + '</td><td colspan="4" style="text-align:left">行情缺失</td></tr>';
        return "<tr" + (x.self ? ' class="p1-self"' : "") + "><td><b>" + esc(x.name) + "</b>" + (x.self ? ' <span class="p1-sub">本股</span>' : "") + "</td><td>" + (x.pe > 0 ? n2(x.pe, 1) : '<span class="p1-sub">亏损</span>') + "</td><td>" + n2(x.pb, 2) + "</td><td>" + (x.dy == null ? "—" : n2(x.dy, 2) + "%") + "</td><td>" + (x.cap == null ? "—" : Math.round(x.cap).toLocaleString("en-US")) + "</td></tr>";
      }).join("") + '<tr class="p1-dim"><td>同行中位</td><td>' + n2(pe.median.pe, 1) + "</td><td>" + n2(pe.median.pb, 2) + '</td><td colspan="2"></td></tr></tbody></table></div>' +
      '<div class="p1-legend">比同行中位低 20% 以上记 +1，高 30% 以上记 −1。</div>';
    how += '<div class="va-h">结论规则</div><div class="p1-legend">三把尺子相加（满分 ±5）：≥4 明显便宜 · ≥2 偏便宜 · ≤−2 偏贵 · ≤−4 明显偏贵，其余合理。3 把都能用 = 可信度高；2 把能用或只能看市销率 = 中；只有 1 把 = 低，结论最多到「偏」。' +
      (d.warn && d.warn.length ? "<br>提示：" + d.warn.map(esc).join("；") : "") + "<br>口径：" + esc(d.method) + "。最近财报期 " + esc(d.last_report) + "。</div>";
    html += '<details class="dc-fold va-how" data-fold="va-how:' + esc(code) + '"><summary>怎么算的<span class="sub">分位表 · DCF 情景 · 同行明细</span></summary><div class="va-how-b">' + how +
      '<div class="fbar"><span class="nt-hint"></span><div class="right"><button class="nt-ghost" id="valRefresh" data-code="' + esc(code) + '">重新计算</button></div></div></div></details>';
    return html;
  };
  P1.bindValuation = function () {
    on("[data-valm]", "click", function (el) {
      var st = S(); st.valM = st.valM || {}; st.valM[el.getAttribute("data-code")] = el.getAttribute("data-valm"); W().renderMain();
    });
    var b = byId("valRefresh"); if (!b) return;
    b.addEventListener("click", function () { b.disabled = true; P1.loadVal(b.getAttribute("data-code"), true); W().renderMain(); });
  };

  /* ================= P1-2 事件重要性评分 ================= */
  P1.loadEvScores = function (force) { load("evs", "/api/events/scores", null, force); };
  P1.evScores = function (code) { P1.loadEvScores(false); var d = S().evs; return (d && d.items && d.items[code]) || {}; };
  P1.impBadge = function (c) {
    /* 用户要求：不显示分数；仅 ≥4 分显示「重要」标签（分数与依据放悬停提示） */
    var sc = c.score || {}, imp = c.imp || 0;
    if (imp < 4) return "";
    var cl = imp >= 5 ? "p1-red" : "p1-amber";
    var tip = "重要性 " + imp + "/5（" + (sc.src === "rule" ? "规则" : "AI") + "）\n" + (sc.why || "") + "\n传导：" + (sc.chain || "") + " · " + (sc.horizon || "");
    return '<span class="p1-badge p1-imp ' + cl + '" title="' + esc(tip) + '">重要' + (sc.src && sc.src !== "rule" ? " · AI" : "") + "</span>";
  };
  P1.tlToolbar = function (tl) {
    var st = W().state, chip = function (attr, on, label) { return '<button class="ev-chip' + (on ? " on" : "") + '" ' + attr + '="1">' + esc(label) + "</button>"; };
    var d = S().evs || {}, busy = S().evScoring;
    return '<span class="grp-lb">重要性</span><div class="ev-chips">' + chip("data-tlsort", st.tlSort === "imp", "按重要性排序") +
      chip("data-tlimp", st.tlImp === 4, "只看 ≥4 分" + (tl.stats.high ? "（" + tl.stats.high + "）" : "")) +
      '<button class="ev-chip" id="evScoreBtn" data-code="' + esc(tl.code) + '"' + (busy ? " disabled" : "") + ' title="用 DeepSeek 为近 30 天未评分事件打分（≤20 条/次），覆盖规则分">' + (busy ? "AI 评分中…" : "AI 评分") + "</button></div>";
  };
  P1.bindEvScore = function () {
    var st = W().state;
    on("[data-tlsort]", "click", function () { st.tlSort = st.tlSort === "imp" ? "" : "imp"; W().renderMain(); });
    on("[data-tlimp]", "click", function () { st.tlImp = st.tlImp === 4 ? 0 : 4; W().renderMain(); });
    var b = byId("evScoreBtn"); if (!b) return;
    b.addEventListener("click", function () {
      S().evScoring = true; W().renderMain();
      W().apiPost("/api/events/score", { code: b.getAttribute("data-code"), days: 30 }).then(function (r) {
        S().evScoring = false;
        W().toast(r && r.ok ? "AI 评分完成：" + (r.scored || 0) + " 条" + (r.note ? "（" + r.note + "）" : "") : "AI 评分失败：" + ((r && r.error) || "未知错误"));
        P1.loadEvScores(true);
      });
    });
  };


  /* 表单有未保存内容时阻止轮询重绘（纪律卡/理由/流水草稿不丢） */
  P1.dirty = function () {
    var v = function (id) { var e = byId(id); return e && String(e.value || "").trim(); };
    if (v("plTitle") || v("plDiscReason") || v("trReason") || v("trPrice") || v("trShares") || v("trAmount") || v("trDiscReason")) return true;
    var b = byId("plDisc"); return !!(b && b.querySelector(".p1-disc"));
  };
  window.P1 = P1;
})();
