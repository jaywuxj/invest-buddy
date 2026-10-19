/*
 * hedit.js — 持仓 / 关注增删改弹窗（#76 #79）
 * 入口：HEdit.open("pos"|"watch", code?)  —— 无 code 为新增，有 code 为编辑
 * 口径：股数+成本同时填写才计入仓位比例 / 盈亏 / 红线；都留空只看行情
 * 服务端：POST /api/holdings（与交易流水同步），GET /api/stock_search
 */
(function () {
  "use strict";
  var W = function () { return window.WB; };
  var esc = function (s) { return W().esc(s); };
  var st = null; // { kind, code, pick, confirmForce }

  function mask() {
    var m = document.getElementById("heMask");
    if (!m) {
      m = document.createElement("div"); m.id = "heMask"; m.className = "he-mask"; document.body.appendChild(m);
      m.addEventListener("mousedown", function (e) { if (e.target === m) close(); });
      document.addEventListener("keydown", function (e) { if (e.key === "Escape" && m.classList.contains("on")) close(); });
    }
    return m;
  }
  function close() { var m = document.getElementById("heMask"); if (m) { m.classList.remove("on"); m.innerHTML = ""; } st = null; }
  function $(id) { return document.getElementById(id); }
  function val(id) { var el = $(id); return el ? el.value.trim() : ""; }

  function open(kind, code) {
    var s = code ? W().findStock(code) : null;
    st = { kind: kind, code: code || null, pick: null, confirmForce: false };
    var isPos = kind === "pos", edit = !!s;
    var title = (edit ? "编辑" : "添加") + (isPos ? "持仓" : "关注");
    var h = '<div class="he-modal" role="dialog" aria-label="' + title + '"><button class="he-x" data-he="close" aria-label="关闭">✕</button><h3>' + title + "</h3>";
    if (edit) {
      h += '<div class="he-stock"><b>' + esc(s.name) + '</b><span>' + esc(s.code) + " · " + esc(s.market || "") + " · " + esc(s.currency || "") + "</span></div>";
    } else {
      h += '<label class="he-f"><span>股票</span><div class="he-srch"><input id="heQ" autocomplete="off" placeholder="代码 / 名称 / 拼音，如 600519、茅台、NVDA"><div id="heRes" class="he-res"></div></div></label>' +
        '<div id="hePick" class="he-pick"></div>';
    }
    h += '<label class="he-f"><span>显示名称</span><input id="heName" value="' + esc(s ? s.name : "") + '" maxlength="30"></label>';
    if (isPos) {
      var sh = s && s.shares != null ? s.shares : "", co = s && s.cost != null ? s.cost : "";
      h += '<div class="he-row2"><label class="he-f"><span>持股数量</span><input id="heShares" type="number" min="0" step="any" inputmode="decimal" value="' + esc(sh) + '" placeholder="选填"></label>' +
        '<label class="he-f"><span>成本价（每股 · <em id="heCur">' + esc(s ? s.currency : "本币") + '</em>）</span><input id="heCost" type="number" min="0" step="any" inputmode="decimal" value="' + esc(co) + '" placeholder="选填"></label></div>' +
        '<div class="he-hint">两项都填才计入仓位比例与盈亏；留空则只看行情。' +
        (edit ? "有买卖流水的持仓请到「交易」Tab 记一笔调整。" : "填写后自动生成期初流水。") + "</div>";
    }
    h += '<label class="he-f"><span>板块</span><input id="heSector" value="' + esc(s && s.sector && s.sector !== "其他" ? s.sector : "") + '" maxlength="30" placeholder="如 半导体、互联网/AI（用于行业集中度）"></label>' +
      '<label class="he-f"><span>备注</span><textarea id="heNote" rows="2" maxlength="200" placeholder="关注理由 / 持仓逻辑">' + esc(s ? s.note || "" : "") + "</textarea></label>" +
      '<div id="heErr" class="he-err"></div><div class="he-act">' +
      (edit ? '<button class="he-del" data-he="del">' + (isPos ? "删除持仓" : "取消关注") + "</button>" : "") +
      (edit && isPos ? '<label class="he-tow"><input type="checkbox" id="heToWatch" checked> 删除后保留在关注</label>' : "") +
      '<span class="he-sp"></span><button class="he-cancel" data-he="close">取消</button><button class="he-ok" data-he="save"' + (edit ? "" : " disabled") + ">" + (edit ? "保存" : "添加") + "</button></div></div>";
    var m = mask(); m.innerHTML = h; m.classList.add("on");
    Array.prototype.forEach.call(m.querySelectorAll('[data-he="close"]'), function (b) { b.onclick = close; });
    m.querySelector('[data-he="save"]').onclick = save;
    var del = m.querySelector('[data-he="del"]'); if (del) del.onclick = remove;
    if (!edit) bindSearch(); else if ($("heName")) $("heName").focus();
  }

  /* ---- 搜索（防抖 250ms，键盘上下 + 回车选择） ---- */
  function bindSearch() {
    var q = $("heQ"), res = $("heRes"), timer = null, seq = 0, items = [], hi = -1;
    q.focus();
    function paint() {
      if (!items.length) { res.innerHTML = q.value.trim() ? '<div class="he-none">没有匹配的 A 股 / 港股 / 美股 / ETF</div>' : ""; res.classList.toggle("on", !!q.value.trim()); return; }
      res.innerHTML = items.map(function (x, i) {
        var tag = x.in === "pos" ? "已持仓" : x.in === "watch" ? (st.kind === "pos" ? "关注中 · 转为持仓" : "已关注") : "";
        var dis = x.in === "pos" || (x.in === "watch" && st.kind === "watch");
        return '<button type="button" class="he-it' + (i === hi ? " hi" : "") + (dis ? " dis" : "") + '" data-i="' + i + '"' + (dis ? " disabled" : "") + ">" +
          '<span class="n">' + esc(x.name) + '</span><span class="c">' + esc(x.code) + " · " + esc(x.market) + "</span>" + (tag ? '<span class="t">' + tag + "</span>" : "") + "</button>";
      }).join("");
      res.classList.add("on");
      Array.prototype.forEach.call(res.querySelectorAll(".he-it"), function (b) {
        b.onmousedown = function (e) { e.preventDefault(); choose(items[+b.getAttribute("data-i")]); };
      });
    }
    function choose(x) {
      if (!x || x.in === "pos" || (x.in === "watch" && st.kind === "watch")) return;
      st.pick = x; res.classList.remove("on"); q.value = x.name + "  " + x.code;
      $("hePick").innerHTML = '<span>' + esc(x.market) + " · " + esc(x.currency) + (x.kind ? " · " + esc(x.kind) : "") + "</span>" + (x.in === "watch" ? '<span class="he-conv">将从关注移入持仓，保留原备注</span>' : "");
      if (!val("heName")) $("heName").value = x.name;
      if ($("heCur")) $("heCur").textContent = x.currency;
      var ex = x.in === "watch" ? W().findStock(x.code) : null;
      if (ex) { if (!val("heSector") && ex.sector && ex.sector !== "其他") $("heSector").value = ex.sector; if (!val("heNote") && ex.note) $("heNote").value = ex.note; }
      document.querySelector('#heMask [data-he="save"]').disabled = false;
      ($("heShares") || $("heSector")).focus();
    }
    q.addEventListener("input", function () {
      st.pick = null; document.querySelector('#heMask [data-he="save"]').disabled = true; $("hePick").innerHTML = "";
      clearTimeout(timer);
      var v = q.value.trim(); if (!v) { items = []; paint(); return; }
      timer = setTimeout(function () {
        var my = ++seq;
        W().fetchOpt("/api/stock_search?q=" + encodeURIComponent(v)).then(function (r) {
          if (my !== seq || !st) return;
          items = (r && r.items) || []; hi = items.length ? 0 : -1; paint();
        });
      }, 250);
    });
    q.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { if (!items.length) return; e.preventDefault(); hi = (hi + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length; paint(); }
      else if (e.key === "Enter") { e.preventDefault(); if (items[hi]) choose(items[hi]); }
    });
    q.addEventListener("blur", function () { setTimeout(function () { res.classList.remove("on"); }, 120); });
  }

  function err(msg) { var e = $("heErr"); if (e) e.textContent = msg || ""; }
  function busy(on) { Array.prototype.forEach.call(document.querySelectorAll("#heMask .he-act button"), function (b) { b.disabled = on; }); }

  function after(r, msg, gone) {
    close();
    var s = W().state; if (s.p1) s.p1.trades = null; s.diag = null;
    W().toast(msg + (r.trade_note ? "（" + r.trade_note + "）" : ""));
    W().refreshHoldings().then(function () {
      if (gone && s.selected === gone && !W().findStock(gone)) { s.selected = "overview"; s.tab = "ov-diag"; W().renderSidebar(); }
      W().reloadQuotes && W().reloadQuotes();
      W().renderMain();
    });
  }

  function save() {
    var isPos = st.kind === "pos", edit = !!st.code;
    var body = { name: val("heName"), sector: val("heSector"), note: val("heNote") };
    if (isPos) { body.shares = val("heShares"); body.cost = val("heCost"); }
    if (edit) { body.op = isPos ? "update_pos" : "update_watch"; body.code = st.code; }
    else {
      if (!st.pick) return err("请先从搜索结果中选择股票");
      body.op = isPos ? "add_pos" : "add_watch"; body.code = st.pick.code; body.gtimg = st.pick.gtimg;
      if (st.pick.kline) body.kline = st.pick.kline;
    }
    if (isPos && (body.shares === "") !== (body.cost === "")) return err("股数和成本价需同时填写；都留空则不纳入仓位计算");
    busy(true); err("");
    W().apiPost("/api/holdings", body).then(function (r) {
      busy(false);
      if (!r.ok) return err(r.message || r.error || "保存失败");
      var counted = isPos && body.shares !== "";
      after(r, (edit ? "已保存" : "已添加") + (isPos && !counted ? " · 未填股数/成本，不计入仓位比例" : ""));
      if (!edit) {
        var s2 = W().state; s2.selected = r.code; s2.tab = "news";
        if (W().refreshOneStock) W().refreshOneStock(r.code);  /* 新增股票：立即补齐该股全部信息 */
      }
    }).catch(function () { busy(false); err("网络错误，请重试"); });
  }

  function remove() {
    var isPos = st.kind === "pos", code = st.code;
    var body = { op: isPos ? "remove_pos" : "remove_watch", code: code };
    if (isPos) { body.to_watch = !!($("heToWatch") && $("heToWatch").checked); if (st.confirmForce) body.force = true; }
    /* 取消关注：点一次直接生效（关注股无持仓/流水，误删可在「发现」或「＋」里重新加回） */
    busy(true);
    W().apiPost("/api/holdings", body).then(function (r) {
      busy(false);
      if (r.ok) return after(r, isPos ? (r.to_watch ? "已删除持仓，保留在关注" : "已删除持仓") + (r.trades_removed ? " · 移除 " + r.trades_removed + " 笔流水" : "") : "已取消关注", r.to_watch ? null : code);
      if (r.error === "trades_exist") {
        st.confirmForce = true; err(r.message);
        document.querySelector('#heMask [data-he="del"]').textContent = "强制删除（含流水）";
        return;
      }
      err(r.message || r.error || "删除失败");
    }).catch(function () { busy(false); err("网络错误，请重试"); });
  }

  window.HEdit = { open: open, close: close };
})();
