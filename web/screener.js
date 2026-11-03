/* 选股（2026-11-03）：本周精选 / 候选排名 / 跟踪 / 方法与回测。
   数字全部由服务端程序计算（/api/screener）；模型只写推荐理由，页面明确标注。红绿只给收益类数字。 */
(function () {
  var W = function () { return window.WB; };
  var S = function () { return window.WB.state; };
  var C = { data: null, busy: false, err: null, ledger: null, poll: null };
  var IC = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="7" cy="7" r="4.5"/><path d="M10.4 10.4L14 14"/><path d="M5.2 7.6l1.3-1.6 1.2 1 1.4-1.9"/></svg>';
  var TABS = [["picks", "本周精选"], ["cands", "候选排名"], ["track", "跟踪"], ["method", "方法与回测"]];
  var FK = ["value", "quality", "momentum", "growth", "lowvol"];
  var FCN = { value: "价值", quality: "质量", momentum: "动量", growth: "成长", lowvol: "稳健" };

  function esc(s) { return W().esc(s); }
  function n1(v, d) { return v == null || !isFinite(v) ? "--" : Number(v).toFixed(d == null ? 1 : d); }
  function sp(v, d) {
    if (v == null || !isFinite(v)) return "--";
    var t = Number(v).toFixed(d == null ? 1 : d);
    if (Number(t) === 0) return "0%";                       /* 避免「-0%」 */
    return (v > 0 ? "+" : "") + t + "%";
  }
  function cls(v) { return v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : ""; }
  function api(url, body) {
    return fetch(url, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined)
      .then(function (r) { return r.json(); });
  }

  function load(force) {
    if (C.busy || (C.data && !force)) return Promise.resolve(C.data);
    C.busy = true;
    return api("/api/screener").then(function (d) {
      if (d && d.ok) { C.data = d; C.err = null; } else C.err = (d && (d.message || d.error)) || "加载失败";
    }).catch(function (e) { C.err = String(e && e.message || e); }).then(function () {
      C.busy = false;
      var st = C.data && C.data.state;
      if (st && (st.running || st.bt_running)) { if (!C.poll) C.poll = setInterval(function () { load(true); }, 5000); }
      else if (C.poll) { clearInterval(C.poll); C.poll = null; }
      if (window.WB_RENDER_DOCK) window.WB_RENDER_DOCK();
      if (S().selected === "screener") W().renderMain();
      return C.data;
    });
  }
  function loadLedger() {
    return api("/api/screener/ledger").then(function (d) { C.ledger = d; if (S().selected === "screener") W().renderMain(); }).catch(function () {});
  }
  function badge() {
    var d = C.data && C.data.data;
    if (C.data && C.data.state && C.data.state.running) return "更新中";
    if (!d) return "";
    var n = 0; Object.keys(d.markets || {}).forEach(function (k) { n += (d.markets[k].picks || []).length; });
    return n ? n + " 只" : "";
  }
  function mk() { return S().scMk || "hk"; }

  /* ---------- 片段 ---------- */
  function facBars(f) {
    return '<div class="sc-fac">' + FK.map(function (k) {
      var v = f && f[k];
      return '<div class="' + (v == null ? "na" : "") + '"><div class="k"><span>' + FCN[k] + "</span><b>" + (v == null ? "—" : Math.round(v)) + '</b></div><div class="t"><i style="width:' + (v || 0) + '%"></i></div></div>';
    }).join("") + "</div>";
  }
  function facts(p) {
    var a = [];
    if (p.pe != null) a.push("PE <b>" + n1(p.pe) + "</b>");
    if (p.pb != null) a.push("PB <b>" + n1(p.pb, 2) + "</b>");
    if (p.dy != null) a.push("股息率 <b>" + n1(p.dy) + "%</b>");
    if (p.roe != null) a.push("ROE <b>" + n1(p.roe) + "%</b>");
    if (p.npm != null) a.push("净利率 <b>" + n1(p.npm) + "%</b>");
    if (p.revg != null) a.push('营收同比 <b class="' + cls(p.revg) + '">' + sp(p.revg) + "</b>");
    if (p.npg != null) a.push('净利同比 <b class="' + cls(p.npg) + '">' + sp(p.npg) + "</b>");
    if (p.mom != null) a.push('近一年（跳过最近 1 月） <b class="' + cls(p.mom) + '">' + sp(p.mom) + "</b>");
    if (p.hiprox != null) a.push("现价 / 12 个月高点 <b>" + n1(p.hiprox, 0) + "%</b>");
    var v = p.valuation;
    if (v && v.ok) {
      var dc = v.dcf || {};
      a.push("估值 <b>" + esc(v.verdict || "--") + "</b>（可信度" + esc(v.confidence || "--") + "）" +
        ((v.hist || {}).rank != null ? " · 历史分位 <b>" + v.hist.rank + "%</b>" : "") +
        (dc.mos != null ? (Math.abs(dc.mos) <= 0.8 ? ' · 安全边际 <b class="' + cls(dc.mos) + '">' + sp(dc.mos * 100, 0) + "</b>" : " · 反向 DCF 不适用") : ""));
    } else if (v) a.push("估值：" + esc(v.reason || "数据不足"));
    var an = p.analyst;
    if (an && an.count) a.push("机构 <b>" + an.count + "</b> 家 · 买入 <b>" + n1(an.buy_pct, 0) + "%</b>" +
      (an.upside != null ? ' · 目标价空间 <b class="' + cls(an.upside) + '">' + sp(an.upside, 0) + "</b>（仅参考）" : ""));
    return '<div class="sc-facts">' + a.map(function (x) { return "<span>" + x + "</span>"; }).join("") + "</div>";
  }
  function stChip(p) {
    var s = p.status;
    if (!s) return "";
    return s.kind === "new" ? '<span class="sc-chip st new">本周新进</span>' : '<span class="sc-chip st">连续 ' + s.weeks + " 周</span>";
  }
  function tagChips(tags) {
    return (tags || []).map(function (t) { return '<span class="sc-chip' + (t === "严重低估" ? " hot" : "") + '" title="' + esc(tagDef(t)) + '">' + esc(t) + "</span>"; }).join("");
  }
  function tagDef(t) {
    if (t === "严重低估") return "估值三把尺子复核为「明显便宜」，或「偏便宜」且反向 DCF 安全边际 30%-80%；可信度不是「低」";
    var d = C.data && C.data.data && C.data.data.tag_def; return (d && d[t]) || "";
  }
  function pickCard(p, i) {
    var r = p.reason, soft = (p.flags || []).filter(function (x) { return x.level === "soft"; });
    var h = '<div class="sc-card"><div class="sc-top"><span class="sc-no">' + (i + 1) + '</span><span class="sc-nm">' + esc(p.name) + '</span><span class="sc-cd">' + esc(p.code) + "</span>" +
      (p.industry ? '<span class="sc-cd">' + esc(p.industry) + "</span>" : "") + stChip(p) + tagChips(p.tags) + (p.held ? '<span class="sc-chip held">已' + esc(p.held) + "</span>" : "") +
      '<span class="sc-score"><b>' + n1(p.score, 0) + "</b><span>综合分 · 池内第 " + p.rank + " 名</span></span></div>" + facBars(p.f);
    if (r) {
      h += '<div class="sc-one">' + esc(r.one_line) + '<span class="sc-src">模型撰写 · 数字已与程序数据对账</span></div>' +
        '<div class="sc-cols"><div><div class="h">为什么值得研究</div><ul>' + (r.bull || []).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul></div>" +
        '<div><div class="h">主要风险</div><ul>' + (r.risks || []).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul></div></div>" +
        '<div class="sc-line"><b>证伪条件：</b>' + esc((r.falsify || []).join("；")) + "</div>" +
        (r.watch ? '<div class="sc-line"><b>接下来核实：</b>' + esc(r.watch) + "</div>" : "");
    } else h += '<div class="sc-noreason">推荐理由暂未生成（模型调用失败或未配置），下面的程序数据不受影响。</div>';
    if (soft.length) h += '<div class="sc-flag">' + soft.map(function (x) { return "<b>" + esc(x.k) + "</b>：" + esc(x.why); }).join("<br>") + "</div>";
    h += facts(p);
    h += '<div class="sc-act">' + (p.held ? '<span class="mut">已在' + esc(p.held) + "里</span>" :
      '<button class="pri" data-sc-watch="' + esc(p.code) + '">加入关注</button>') +
      '<button data-sc-ign="' + esc(p.code) + '">' + (p.ignored ? "取消忽略" : "不感兴趣") + "</button>" +
      '<span class="sp"></span><span class="mut">入选时价格 ' + n1(p.px, 2) + "</span></div></div>";
    return h;
  }

  /* ---------- 各 Tab ---------- */
  function renderPicks(d) {
    var m = d.markets[mk()];
    if (!m) return '<div class="sc-empty">本周' + (mk() === "hk" ? "港股" : "美股") + "取数失败：" + esc((d.errors || []).join("；")) + "</div>";
    var bt = C.data.backtest && C.data.backtest[mk()];
    var h = "";
    if (bt && bt.summary) {
      var s = bt.summary, lv = { "有效": "eff", "弱有效": "weak", "无优势": "none", "反向": "rev" }[s.verdict] || "none";
      h += '<div class="sc-banner"><span class="lv ' + lv + '">回测：' + esc(s.verdict) + "</span><span>同一套打分在过去 <b>" + s.months + "</b> 个月里，" +
        "前 " + (C.data.backtest_meta.top_n) + " 名每月平均" + (s.top_ex1 >= 0 ? "跑赢" : "跑输") + "同池等权 <b>" + n1(Math.abs(s.top_ex1), 2) + "</b> 个百分点，" + s.win + "% 的月份跑赢。" +
        "这是筛选「值得研究的名单」，不是买入信号。<a data-sc-tab=\"method\">看回测细节 ›</a></span></div>";
    } else h += '<div class="sc-banner"><span class="lv none">回测：未完成</span><span>还没有回测结果，先别把名单当成有效信号。<a data-sc-tab="method">去跑回测 ›</a></span></div>';
    h += (m.picks || []).map(pickCard).join("") || '<div class="sc-empty">本周没有通过全部条件的标的。</div>';
    if ((m.exited || []).length) h += '<div class="sc-sub"><div class="h">本周退出</div>' + m.exited.map(function (x) {
      return '<div class="r">' + esc(x.name) + "（在榜 " + (x.weeks || 1) + " 周）：" + esc(x.why) + "</div>"; }).join("") + "</div>";
    if ((m.dropped || []).length) h += '<div class="sc-sub"><div class="h">综合分靠前、但没进精选</div>' + m.dropped.map(function (x) {
      return '<div class="r">' + esc(x.name) + " " + esc(x.code) + " · 综合分 " + n1(x.score, 0) + "：" + esc(x.why) + "</div>"; }).join("") + "</div>";
    return h;
  }
  function pcCell(v) {
    if (v == null) return '<td class="pc"><i>—</i></td>';
    var a = Math.max(0.06, v / 100 * 0.32);
    return '<td class="pc"><i style="background:rgba(47,95,163,' + a.toFixed(2) + ')">' + Math.round(v) + "</i></td>";
  }
  function renderCands(d) {
    var m = d.markets[mk()];
    if (!m) return '<div class="sc-empty">本周取数失败。</div>';
    var ft = S().scTag || "", hideHard = S().scHideHard !== false;
    var rows = (m.candidates || []).filter(function (r) {
      if (hideHard && (r.flags || []).some(function (x) { return x.level === "hard"; })) return false;
      return !ft || (r.tags || []).indexOf(ft) >= 0;
    });
    var tags = Object.keys(d.tag_def || {});
    var h = '<div class="sc-bar"><div class="sc-filter"><button class="' + (!ft ? "on" : "") + '" data-sc-tag="">全部</button>' + tags.map(function (t) {
      return '<button class="' + (ft === t ? "on" : "") + '" data-sc-tag="' + esc(t) + '" title="' + esc(d.tag_def[t]) + '">' + esc(t) + "</button>"; }).join("") +
      '</div><span class="sp"></span><label class="sc-hint"><input type="checkbox" data-sc-hard' + (hideHard ? " checked" : "") + "> 隐藏有硬伤的（亏损 / 利润大跌 / 急跌）</label></div>";
    h += '<div class="sc-tblwrap"><table class="sc-tbl"><tr><th>#</th><th class="l">公司</th><th>综合</th>' + FK.map(function (k) { return "<th>" + FCN[k] + "</th>"; }).join("") +
      "<th>PE</th><th>股息率</th><th>ROE</th><th>近一年</th><th class=\"l\">标签 / 提示</th><th></th></tr>" +
      rows.map(function (r) {
        var fl = (r.flags || []).map(function (x) { return x.k; }).join("、");
        return '<tr class="' + (r.held || r.ignored ? "dim" : "") + '"><td>' + r.rank + '</td><td class="l">' + esc(r.name) + '<span class="n2">' + esc(r.code) + "</span></td><td><b>" + n1(r.score, 0) + "</b></td>" +
          FK.map(function (k) { return pcCell(r.f && r.f[k]); }).join("") +
          "<td>" + n1(r.pe) + "</td><td>" + (r.dy != null ? n1(r.dy) + "%" : "--") + "</td><td>" + (r.roe != null ? n1(r.roe) + "%" : "--") + '</td><td class="' + cls(r.mom != null && Math.abs(r.mom) < 0.5 ? 0 : r.mom) + '">' + sp(r.mom, 0) + "</td>" +
          '<td class="tg">' + tagChips(r.tags) + (fl ? '<span class="fl">' + esc(fl) + "</span>" : "") + "</td><td>" +
          (r.held ? '<span class="sc-hint">' + esc(r.held) + "</span>" : r.ignored ? '<span class="sc-hint">已忽略</span>' : '<button data-sc-watch="' + esc(r.code) + '">关注</button>') + "</td></tr>";
      }).join("") + "</table></div>";
    h += '<div class="sc-hint" style="margin-top:8px">因子分是「在本市场候选池里的百分位」（0-100，越高越好）；显示综合分前 60 名。候选池：' + esc(m.universe) + "，共 " + m.pool_n + " 只，其中 " + m.hard_n + " 只有硬伤。</div>";
    return h;
  }
  function renderTrack() {
    var L = C.ledger;
    if (!L) { loadLedger(); return '<div class="empty">正在读取跟踪账本…</div>'; }
    if (!L.ok) return '<div class="sc-empty">' + esc(L.message || "账本读取失败") + "</div>";
    var st = L.stats || {}, h = '<div class="sc-banner"><span class="lv none">口径</span><span>每只股票<b>第一次入选</b>记一笔，按入选价买入持有，到 20 / 60 / 120 / 250 个交易日时与' +
      "指数比较（恒指 / 标普 500）。退出名单不影响计算，避免只记住好的。样本少于 10 笔时不下结论。</span></div>";
    h += '<div class="sc-kpis">' + [["d20", "20 个交易日（约 1 月）"], ["d60", "60 个交易日（约 3 月）"], ["d120", "120 个交易日（约半年）"], ["d250", "250 个交易日（约 1 年）"]].map(function (x) {
      var s = st[x[0]] || {};
      return '<div class="sc-kpi"><div class="k">' + x[1] + '</div><div class="v ' + cls(s.ex) + '">' + (s.n ? sp(s.ex, 2) : "--") + '</div><div class="s">' +
        (s.n ? "平均超额 · " + s.n + " 笔 · 跑赢 " + s.hit + "%<br>" : "") + esc(s.verdict || "尚无到期样本") + "</div></div>";
    }).join("") + "</div>";
    var E = L.episodes || [];
    if (!E.length) return h + '<div class="sc-empty">还没有入选记录。每周六更新一次精选后，这里开始累积。</div>';
    h += '<div class="sc-bar"><span class="sc-hint">账本更新于 ' + esc(String(L.updated_at || "--").slice(0, 16).replace("T", " ")) + '</span><span class="sp"></span><button class="sc-run" data-sc-ledger>刷新收益</button></div>';
    h += '<div class="sc-tblwrap"><table class="sc-tbl"><tr><th class="l">公司</th><th class="l">入选日</th><th>入选价</th><th class="l">状态</th><th>至今</th><th>同期指数</th><th>超额</th><th>20 日超额</th><th>60 日超额</th><th>120 日超额</th></tr>' +
      E.map(function (e) {
        var p = e.perf || {}, nw = e.now || {};
        var cell = function (k) { var x = p[k]; return x ? '<td class="' + cls(x.ex) + '">' + sp(x.ex) + "</td>" : '<td class="sc-hint">未到期</td>'; };
        return '<tr><td class="l">' + esc(e.name) + '<span class="n2">' + esc(e.code) + '</span></td><td class="l">' + esc(e.entry_date) + "</td><td>" + n1(e.entry_px, 2) + '</td><td class="l">' +
          (e.status === "active" ? "在榜 " + (e.weeks || 1) + " 周" : '<span title="' + esc(e.exit_reason || "") + '">已退出（' + esc(e.exit_date || "") + "）</span>") + "</td>" +
          '<td class="' + cls(nw.ret) + '">' + sp(nw.ret) + '</td><td class="' + cls(nw.bench) + '">' + sp(nw.bench) + '</td><td class="' + cls(nw.ex) + '"><b>' + sp(nw.ex) + "</b></td>" +
          cell("d20") + cell("d60") + cell("d120") + "</tr>";
      }).join("") + "</table></div>";
    return h;
  }
  function chart(series) {
    if (!series || series.length < 2) return "";
    var keys = [["cum_top", "#1F2329", ""], ["cum_pool", "#8A919C", ""], ["cum_bench", "#2F5FA3", "4 3"]];
    var all = []; series.forEach(function (s) { keys.forEach(function (k) { if (s[k[0]] != null) all.push(s[k[0]]); }); });
    var lo = Math.min(0, Math.min.apply(null, all)), hi = Math.max(0, Math.max.apply(null, all)), Wd = 640, Ht = 200, pad = 28;
    var x = function (i) { return pad + i / (series.length - 1) * (Wd - pad - 8); };
    var y = function (v) { return 8 + (hi - v) / ((hi - lo) || 1) * (Ht - 24); };
    var svg = '<svg viewBox="0 0 ' + Wd + " " + Ht + '" preserveAspectRatio="none"><line x1="' + pad + '" x2="' + (Wd - 8) + '" y1="' + y(0) + '" y2="' + y(0) + '" stroke="#ECEAE4"/>' +
      '<text x="2" y="' + (y(hi) + 4) + '" font-size="10" fill="#8A919C">' + Math.round(hi) + '%</text><text x="2" y="' + (y(lo) + 4) + '" font-size="10" fill="#8A919C">' + Math.round(lo) + "%</text>";
    keys.forEach(function (k) {
      var pts = series.map(function (s, i) { return s[k[0]] == null ? null : x(i).toFixed(1) + "," + y(s[k[0]]).toFixed(1); }).filter(Boolean).join(" ");
      svg += '<polyline fill="none" stroke="' + k[1] + '" stroke-width="1.8"' + (k[2] ? ' stroke-dasharray="' + k[2] + '"' : "") + ' points="' + pts + '"/>';
    });
    svg += '<text x="' + pad + '" y="' + (Ht - 2) + '" font-size="10" fill="#8A919C">' + esc(series[0].d.slice(0, 7)) + '</text><text x="' + (Wd - 60) + '" y="' + (Ht - 2) + '" font-size="10" fill="#8A919C">' + esc(series[series.length - 1].d.slice(0, 7)) + "</text></svg>";
    return svg;
  }
  function renderMethod(d) {
    var bt = C.data.backtest && C.data.backtest[mk()], meta = C.data.backtest_meta, running = C.data.state.bt_running;
    var h = '<div class="sc-sec">回测<em>' + (meta ? "生成于 " + esc(String(meta.generated_at).slice(0, 10)) + " · " + esc(meta.from) + " 至 " + esc(meta.to) + " 每月末" : "尚未运行") + '</em><span style="flex:1"></span>' +
      '<button class="sc-run" data-sc-bt' + (running ? " disabled" : "") + ">" + (running ? "回测运行中…（约 20-30 分钟）" : meta ? "重新回测" : "运行回测") + "</button></div>";
    if (bt && bt.summary) {
      var s = bt.summary, f = bt.factors || {};
      h += '<div class="sc-kpis">' +
        '<div class="sc-kpi"><div class="k">结论</div><div class="v">' + esc(s.verdict) + '</div><div class="s">' + s.months + " 个独立月份 · " + esc(bt.universe || "") + "</div></div>" +
        '<div class="sc-kpi"><div class="k">前 ' + meta.top_n + ' 名每月超额（vs 同池等权）</div><div class="v ' + cls(s.top_ex1) + '">' + sp(s.top_ex1, 2) + '</div><div class="s">t = ' + n1(s.top_ex1_t, 2) + " · " + s.win + "% 的月份跑赢</div></div>" +
        '<div class="sc-kpi"><div class="k">综合分秩相关 IC</div><div class="v">' + n1((f.score || {}).ic, 3) + '</div><div class="s">t = ' + n1((f.score || {}).t, 2) + " · " + (f.score || {}).pos + "% 月份为正</div></div>" +
        '<div class="sc-kpi"><div class="k">累计（前 ' + meta.top_n + ' / 同池 / 指数）</div><div class="v ' + cls(s.cum_top) + '">' + sp(s.cum_top, 0) + '</div><div class="s">同池 ' + sp(s.cum_pool, 0) + " · " + esc(bt.bench_name) + " " + sp(s.cum_bench, 0) + "</div></div>" +
        '<div class="sc-kpi"><div class="k">未来 6 个月超额（参考）</div><div class="v ' + cls(s.top_ex6) + '">' + sp(s.top_ex6, 1) + '</div><div class="s">窗口重叠，有效样本约 ' + s.n6_eff + " 个</div></div></div>";
      h += '<div class="sc-chart">' + chart(bt.series) + '<div class="sc-legend"><span><i style="background:#1F2329"></i>前 ' + meta.top_n + ' 名（每月换仓、等权、未扣成本）</span><span><i style="background:#8A919C"></i>同一候选池等权</span><span><i style="background:#2F5FA3"></i>' + esc(bt.bench_name) + "</span></div></div>";
      h += '<div class="sc-sec">各因子单独看<em>当月因子分与下月收益的秩相关；|t| ≥ 2 才算显著</em></div><div class="sc-tblwrap"><table class="sc-tbl"><tr><th class="l">因子</th><th>权重</th><th>平均 IC</th><th>t</th><th>为正的月份</th><th class="l">结论</th></tr>' +
        ["score"].concat(FK).map(function (k) {
          var x = f[k] || {}; if (!x.n) return "";
          var t = x.t || 0, word = t >= 2 ? "显著有效" : t <= -2 ? "显著反向" : t >= 1 ? "偏正，不显著" : t <= -1 ? "偏负，不显著" : "接近随机";
          return '<tr><td class="l">' + (k === "score" ? "<b>综合分</b>" : FCN[k]) + "</td><td>" + (k === "score" ? "—" : Math.round(((C.data.data && C.data.data.weights) || {})[k] * 100 || 0) + "%") + "</td><td>" + n1(x.ic, 3) + "</td><td>" + n1(x.t, 2) + "</td><td>" + x.pos + '%</td><td class="l">' + word + "</td></tr>";
        }).join("") + "</table></div>";
      var tg = bt.tags || {};
      h += '<div class="sc-sec">各标签单独看<em>带该标签的股票下月平均跑赢同池多少</em></div><div class="sc-tblwrap"><table class="sc-tbl"><tr><th class="l">标签</th><th>月均超额</th><th>t</th><th>有样本的月份</th><th class="l">定义</th></tr>' +
        Object.keys(tg).map(function (k) { var x = tg[k]; return '<tr><td class="l">' + esc(k) + '</td><td class="' + cls(x.ex1) + '">' + sp(x.ex1, 2) + "</td><td>" + n1(x.t, 2) + "</td><td>" + x.n + '</td><td class="l" style="white-space:normal">' + esc((d && d.tag_def || {})[k] || "") + "</td></tr>"; }).join("") + "</table></div>";
      h += '<div class="sc-sec">需要知道的局限</div><ul class="sc-notes">' + (s.notes || []).concat([
        "已退市股票拿不到月 K 会被剔除（幸存者偏差，回测会偏乐观）",
        "数据源的历史财务字段按报告期末回填（会提前看到还没披露的财报），回测已统一滞后 3 个月使用，估值倍数按当月价格重建；没有做行业中性",
        "先验权重来自学术文献，没有按这段回测去调参——调了会更好看，但那是过拟合"]).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul>";
    }
    h += '<div class="sc-sec">方法</div><div class="sc-meth">' +
      "<h4>① 候选池</h4>港股市值 ≥100 亿港元、美股 ≥100 亿美元（剔除场外 ADR、优先股、同名多份额）。每周六 10:00 用 westock 取全市场快照，价格与动量取自腾讯月 K。" +
      "<h4>② 五个因子（同一市场内取百分位，0-100）</h4><div class=\"f\">" +
      "<b>价值</b><span>30%</span><span>盈利收益率 E/P、（港股）B/P 与 S/P、股息率。亏损股 E/P 记最低。——格雷厄姆、Fama-French <a data-sc-learn=\"valuation/pe-pct\">学堂：历史分位</a></span>" +
      "<b>质量</b><span>25%</span><span>ROE（封顶 60%，>100% 视为回购放大不计）、净利率、毛利率。——Novy-Marx 毛利因子、AQR 质量因子 <a data-sc-learn=\"financials/roic\">学堂：回报率</a></span>" +
      "<b>动量</b><span>25%</span><span>过去 12 个月涨幅（跳过最近 1 个月，避开短期反转）+ 现价距 12 个月高点。——Jegadeesh-Titman、George-Hwang</span>" +
      "<b>成长</b><span>10%</span><span>营收、归母净利同比（仅港股；美股选股接口没有这两个字段，权重按比例分给其他因子）。——林奇 GARP <a data-sc-learn=\"valuation/peg\">学堂：PEG</a></span>" +
      "<b>稳健</b><span>10%</span><span>近 12 个月月度波动率，越低越好。——低波动异象</span></div>" +
      "<h4>③ 硬伤排除</h4>亏损、PE &lt; 3（多为一次性收益）、归母净利同比 &lt; -30%（价值陷阱）、过去一年跌超 40%（接飞刀）、美股合伙制 MLP（分红预扣税高、需 K-1 报税）——这些不进精选，只在候选排名里能看到。" +
      "<h4>④ 估值复核与行业分散</h4>综合分前 12 名（每个市场）逐只跑估值三把尺子（历史分位 / 反向 DCF / 同行），「偏贵」的剔除；估值结论「明显便宜」（或「偏便宜」且安全边际 30%-80%）、可信度不低的标「严重低估」。同一行业最多入选 2 只（回测里没有这一步）。<a data-sc-learn=\"valuation/mos\">学堂：安全边际</a>" +
      "<h4>⑤ 推荐理由</h4>模型只拿程序算好的事实写理由、风险和证伪条件，文中每个数字都要能在程序数据里对上，对不上整批作废。" +
      "<h4>⑥ 长期跟踪</h4>每只首次入选记一笔，按入选价跟踪到 250 个交易日，和指数比；退出名单也继续算，避免只记好的。<a data-sc-learn=\"discipline/evidence\">学堂：用证据判断方法有没有用</a></div>";
    return h;
  }

  function renderMain() {
    var st = S();
    if (!C.data) {
      if (!C.busy && !C.err) load();
      return '<div class="empty">' + (C.err ? "选股加载失败：" + esc(C.err) : "正在加载选股…") + "</div>";
    }
    var tab = TABS.some(function (t) { return t[0] === st.tab; }) ? st.tab : "picks";
    st.tab = tab;
    try { history.replaceState(null, "", "#screener/" + tab); } catch (e) {}
    var d = C.data.data, sta = C.data.state || {};
    var note = d ? "上次更新 " + esc(String(d.generated_at).slice(0, 16).replace("T", " ")) + " · 每周六 10:00 自动更新 · " +
      Object.keys(d.markets).map(function (k) { return d.markets[k].name + "候选 " + d.markets[k].pool_n + " 只"; }).join(" / ") : "还没有运行过";
    var h = '<div class="stock-sticky"><div class="stock-head"><div><h1>选股</h1><div class="sc-head-note">' + note + "</div></div>" +
      '<button class="sc-run" data-sc-run' + (sta.running ? " disabled" : "") + ">" + (sta.running ? "更新中：" + esc(sta.stage || "…") : "立即更新") + "</button></div>" +
      '<div class="tabs">' + TABS.map(function (t) { return '<button class="tab' + (t[0] === tab ? " active" : "") + '" data-sc-tab="' + t[0] + '">' + t[1] + "</button>"; }).join("") + "</div></div>";
    if (!C.data.westock) return h + '<div class="sc-empty">选股需要本机安装 westock CLI（腾讯自选股数据，免费）。未检测到 ' + "~/.local/bin/westock，暂时无法取数。</div>";
    if (sta.error) h += '<div class="sc-flag" style="margin-bottom:12px">上次更新失败：' + esc(sta.error) + "</div>";
    if (tab !== "track") {
      h += '<div class="sc-bar"><div class="sc-seg"><button class="' + (mk() === "hk" ? "on" : "") + '" data-sc-mk="hk">港股</button><button class="' + (mk() === "us" ? "on" : "") + '" data-sc-mk="us">美股</button></div>' +
        '<span class="sp"></span><span class="sc-hint">筛出来的是「值得研究的名单」，不是买入建议</span></div>';
    }
    if (tab === "method") return h + renderMethod(d);
    if (tab === "track") return h + renderTrack();
    if (!d) return h + '<div class="sc-empty">还没有选股结果。点右上角「立即更新」开始第一次筛选（约 5-10 分钟：取两个市场快照、逐只估值复核、生成推荐理由）。<br>' +
      '<button class="sc-run" data-sc-run' + (sta.running ? " disabled" : "") + ">" + (sta.running ? "更新中：" + esc(sta.stage) : "立即更新") + "</button></div>";
    return h + (tab === "cands" ? renderCands(d) : renderPicks(d));
  }

  function findRow(code) {
    var d = C.data && C.data.data, hit = null;
    Object.keys((d && d.markets) || {}).forEach(function (k) {
      (d.markets[k].picks || []).concat(d.markets[k].candidates || []).forEach(function (r) { if (r.code === code && !hit) hit = r; });
    });
    return hit;
  }
  function bind() {
    var main = document.getElementById("main"), st = S();
    var on = function (sel, fn) { Array.prototype.forEach.call(main.querySelectorAll(sel), function (el) { el.addEventListener("click", function (e) { fn(el, e); }); }); };
    on("[data-sc-tab]", function (el) { st.tab = el.getAttribute("data-sc-tab"); W().renderMain(); window.scrollTo(0, 0); });
    on("[data-sc-mk]", function (el) { st.scMk = el.getAttribute("data-sc-mk"); W().renderMain(); });
    on("[data-sc-tag]", function (el) { st.scTag = el.getAttribute("data-sc-tag"); W().renderMain(); });
    var hd = main.querySelector("[data-sc-hard]"); if (hd) hd.addEventListener("change", function () { st.scHideHard = hd.checked; W().renderMain(); });
    on("[data-sc-run]", function (el) {
      el.disabled = true;
      api("/api/screener/run", {}).then(function (r) {
        if (!r || !r.ok) { W().toast("启动失败：" + ((r && (r.message || r.error)) || "网络异常")); el.disabled = false; return; }
        W().toast("选股已开始，约 5-10 分钟，可以先去做别的"); load(true);
      });
    });
    on("[data-sc-bt]", function (el) {
      el.disabled = true;
      api("/api/screener/backtest/run", {}).then(function () { W().toast("回测已在后台运行（约 20-30 分钟）"); load(true); });
    });
    on("[data-sc-ledger]", function (el) {
      el.disabled = true; el.textContent = "刷新中…";
      api("/api/screener/ledger/refresh", {}).then(function () { return loadLedger(); });
    });
    on("[data-sc-learn]", function (el) {
      var p = el.getAttribute("data-sc-learn").split("/");
      st.selected = "learn"; st.tab = p[0]; st.learnLesson = p[1];
      W().renderSidebar(); W().renderMain(); window.scrollTo(0, 0);
    });
    on("[data-sc-ign]", function (el) {
      var code = el.getAttribute("data-sc-ign"), r = findRow(code), want = !(r && r.ignored);
      api("/api/screener/ignore", { code: code, on: want }).then(function (x) {
        if (!x || !x.ok) { W().toast("操作失败"); return; }
        W().toast(want ? "已忽略，下周起不再推荐" : "已取消忽略"); load(true);
      });
    });
    on("[data-sc-watch]", function (el) {
      var code = el.getAttribute("data-sc-watch"), r = findRow(code);
      if (!r) return;
      el.disabled = true;
      var body = { op: "add_watch", code: r.code, gtimg: r.ws, name: r.name };
      if (r.kline_sym && /^us/.test(r.ws)) body.kline = r.kline_sym;   /* 美股需要交易所后缀才能取 K 线 */
      W().apiPost("/api/holdings", body).then(function (x) {
        if (!x || x.ok === false) { W().toast("加入失败：" + ((x && (x.message || x.error)) || "网络异常")); el.disabled = false; return; }
        W().toast("已加入关注，正在补齐「" + r.name + "」的数据…");
        var done = function () { load(true); if (W().refreshOneStock) W().refreshOneStock(r.code); };
        if (W().refreshHoldings) W().refreshHoldings().then(done, done); else done();
      });
    });
  }

  window.SCREENER = { load: load, renderMain: renderMain, bind: bind, badge: badge, icon: IC,
    _inject: function (d) { C.data = d; }, _ledger: function (l) { C.ledger = l; } };   /* _inject/_ledger 仅供离线冒烟测试 */
})();
