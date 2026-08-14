/* 单股回测 + 基本面/红利低波选股系统 - 前端逻辑 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const form = $("#backtest-form");
  const loading = $("#loading");
  const errorBox = $("#error");
  const resultBox = $("#result");
  const runBtn = $("#run-btn");
  const stockInput = $("#ts_code");
  const stockList = $("#stock-list");
  const tsTip = $("#ts-tip");

  // 基本面选股元素
  const screenForm = $("#screen-form");
  const screenBtn = $("#screen-btn");
  const screenResyncBtn = $("#screen-resync-btn");
  const screenLoading = $("#screen-loading");
  const screenError = $("#screen-error");
  const screenResult = $("#screen-result");

  // 红利低波选股元素
  const rlvForm = $("#rlv-form");
  const rlvBtn = $("#rlv-btn");
  const rlvResyncBtn = $("#rlv-resync-btn");
  const rlvLoading = $("#rlv-loading");
  const rlvError = $("#rlv-error");
  const rlvResult = $("#rlv-result");

  // 区间交易参数估算元素
  const bandForm = $("#band-form");
  const bandBtn = $("#band-btn");
  const bandLoading = $("#band-loading");
  const bandError = $("#band-error");
  const bandResult = $("#band-result");
  const bandInput = $("#band_ts_code");
  const bandTip = $("#band-ts-tip");

  // 财报分析元素
  const caibaoForm = $("#caibao-form");
  const caibaoBtn = $("#caibao-btn");
  const caibaoLoading = $("#caibao-loading");
  const caibaoError = $("#caibao-error");
  const caibaoResult = $("#caibao-result");
  const caibaoInput = $("#caibao_ts_code");
  const caibaoTip = $("#caibao-ts-tip");

  // ETF 筛选元素
  const etfForm = $("#etf-form");
  const etfBtn = $("#etf-btn");
  const etfRefreshBtn = $("#etf-refresh-btn");
  const etfLoading = $("#etf-loading");
  const etfError = $("#etf-error");
  const etfResult = $("#etf-result");
  const etfTable = $("#etf-table");
  const etfSub = $("#etf-sub");
  let etfItems = [];
  let etfSort = { key: "scale", order: "desc" };

  // 我的股票元素
  const myRefreshBtn = $("#my-refresh-btn");
  const myLoading = $("#my-loading");
  const myError = $("#my-error");
  const myResult = $("#my-result");
  const mySub = $("#my-sub");

  // Alpha158 回测元素
  const a158Form = $("#alpha158-form");
  const a158Btn = $("#a158-btn");
  const a158Loading = $("#a158-loading");
  const a158Error = $("#a158-error");
  const a158Result = $("#a158-result");
  const a158PoolBox = $("#a158-pool");

  // 组合回测元素
  const ptfForm = $("#ptf-form");
  const ptfBtn = $("#ptf-btn");
  const ptfLoading = $("#ptf-loading");
  const ptfError = $("#ptf-error");
  const ptfResult = $("#ptf-result");
  const ptfPoolBox = $("#ptf-pool");

  let chart = null;
  let quoteChart = null;
  let bandChart = null;
  let alphaChart = null;
  let alphaDdChart = null;
  let ptfChart = null;
  let searchTimer = null;

  // ---------- 工具 ----------
  function fmtPct(v, digits = 2) {
    return (v >= 0 ? "+" : "") + v.toFixed(digits) + "%";
  }
  function fmtNum(v) {
    return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  }
  function fmtDate(ymd) {
    if (!ymd || ymd.length !== 8) return ymd;
    return `${ymd.slice(0, 4)}-${ymd.slice(4, 6)}-${ymd.slice(6, 8)}`;
  }
  function cls(v) { return v >= 0 ? "pos" : "neg"; }

  // ---------- 健康检查 ----------
  async function checkHealth() {
    const el = $("#health");
    try {
      const r = await fetch("/api/health");
      if (r.ok) {
        el.classList.add("ok");
        $("#health-text").textContent = "服务已连接";
      } else throw new Error();
    } catch (e) {
      el.classList.add("err");
      $("#health-text").textContent = "后端服务未启动";
    }
  }

  // ---------- 股票联想 (共享 datalist, 回测/区间估价/财报 三处复用) ----------
  function bindStockSuggest(input, tip) {
    input.addEventListener("input", () => {
      clearTimeout(searchTimer);
      const kw = input.value.trim();
      if (!kw) { stockList.innerHTML = ""; if (tip) tip.textContent = ""; return; }
      searchTimer = setTimeout(async () => {
        try {
          const r = await fetch(`/api/stock/search?keyword=${encodeURIComponent(kw)}`);
          const data = await r.json();
          stockList.innerHTML = (data.items || [])
            .map((it) => `<option value="${it.ts_code}">${it.name} ${it.kind === "fund" ? "[ETF]" : ""} (${it.symbol})</option>`)
            .join("");
        } catch (e) { /* 忽略联想失败 */ }
      }, 300);
    });
  }
  bindStockSuggest(stockInput, tsTip);

  // ---------- 行业联想 (基本面选股 / 红利低波选股) ----------
  // 年份下拉: 2000 ~ 当前年, 默认 2025
  function fillYearSelect(select, def) {
    const cur = new Date().getFullYear();
    let html = "";
    for (let y = cur; y >= 2000; y--) {
      html += `<option value="${y}"${y === def ? " selected" : ""}>${y}年</option>`;
    }
    select.innerHTML = html;
  }
  fillYearSelect($("#sc_year"), 2025);

  // 财报分析: 默认最近 5 个完整财年 (当前年-5 ~ 当前年-1, 如 2026年 → 2021~2025)
  (function initCaibaoYears() {
    const curY = new Date().getFullYear();
    $("#caibao_start_year").value = curY - 5;
    $("#caibao_end_year").value = curY - 1;
  })();

  function bindIndustrySuggest(input, panel) {
    let timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const kw = input.value.trim();
      if (!kw) { panel.innerHTML = ""; panel.classList.add("hidden"); return; }
      timer = setTimeout(async () => {
        try {
          const r = await fetch(`/api/industry/search?keyword=${encodeURIComponent(kw)}&limit=12`);
          const data = await r.json();
          const items = data.items || [];
          panel.innerHTML = items.length
            ? items.map((it) =>
                `<div class="ind-item" data-ind="${it.industry}"><span>${it.industry}</span><span class="ind-count">${it.count} 只</span></div>`).join("")
            : `<div class="ind-empty">无匹配行业</div>`;
          panel.classList.remove("hidden");
        } catch (e) { /* 忽略联想失败 */ }
      }, 200);
    });
    panel.addEventListener("click", (e) => {
      const item = e.target.closest(".ind-item");
      if (!item) return;
      input.value = item.dataset.ind;
      panel.innerHTML = "";
      panel.classList.add("hidden");
      input.dispatchEvent(new Event("change"));
    });
    document.addEventListener("click", (e) => {
      if (!input.contains(e.target) && !panel.contains(e.target)) {
        panel.innerHTML = "";
        panel.classList.add("hidden");
      }
    });
    // 失焦后延迟关闭, 让点击候选能生效
    input.addEventListener("blur", () => {
      setTimeout(() => {
        if (!panel.contains(document.activeElement)) {
          panel.classList.add("hidden");
        }
      }, 150);
    });
  }
  bindIndustrySuggest($("#sc_industry"), $("#sc-ind-panel"));
  bindIndustrySuggest($("#rlv_industry"), $("#rlv-ind-panel"));

  // 输入代码后尝试解析显示名称 + 加载最近行情
  stockInput.addEventListener("change", async () => {
    const code = stockInput.value.trim();
    if (!code) { tsTip.textContent = ""; hideQuote(); return; }
    try {
      const r = await fetch(`/api/stock/${encodeURIComponent(code)}`);
      if (r.ok) {
        const info = await r.json();
        tsTip.classList.add("ok");
        tsTip.classList.remove("err");
        tsTip.textContent = `✓ ${info.name} · ${info.ts_code} · ${info.kind === "fund" ? "基金/ETF" : "股票"}`;
        loadQuote(info.ts_code);
      } else {
        const err = await r.json();
        tsTip.classList.add("err");
        tsTip.classList.remove("ok");
        tsTip.textContent = `✗ ${err.detail || "未找到该代码"}`;
        hideQuote();
      }
    } catch (e) {
      tsTip.textContent = "";
      hideQuote();
    }
  });

  // 区间交易 tab: 股票联想 (共享 datalist) + 名称解析
  bindStockSuggest(bandInput, bandTip);
  bandInput.addEventListener("change", async () => {
    const code = bandInput.value.trim();
    if (!code) { bandTip.textContent = ""; return; }
    try {
      const r = await fetch(`/api/stock/${encodeURIComponent(code)}`);
      if (r.ok) {
        const info = await r.json();
        bandTip.classList.add("ok");
        bandTip.classList.remove("err");
        bandTip.textContent = `✓ ${info.name} · ${info.ts_code} · ${info.kind === "fund" ? "基金/ETF" : "股票"}`;
      } else {
        const err = await r.json();
        bandTip.classList.add("err");
        bandTip.classList.remove("ok");
        bandTip.textContent = `✗ ${err.detail || "未找到该代码"}`;
      }
    } catch (e) { bandTip.textContent = ""; }
  });

  // 财报分析 tab: 股票联想 (共享 datalist) + 名称解析
  bindStockSuggest(caibaoInput, caibaoTip);
  caibaoInput.addEventListener("change", async () => {
    const code = caibaoInput.value.trim();
    if (!code) { caibaoTip.textContent = ""; return; }
    try {
      const r = await fetch(`/api/stock/${encodeURIComponent(code)}`);
      if (r.ok) {
        const info = await r.json();
        caibaoTip.classList.add("ok");
        caibaoTip.classList.remove("err");
        const isHkTip = String(info.ts_code).endsWith(".HK");
        caibaoTip.textContent = `✓ ${info.name} · ${info.ts_code}` + (isHkTip ? " · 东财港股数据" : "");
      } else {
        const err = await r.json();
        caibaoTip.classList.add("err");
        caibaoTip.classList.remove("ok");
        caibaoTip.textContent = `✗ ${err.detail || "未找到该代码"}`;
      }
    } catch (e) { caibaoTip.textContent = ""; }
  });

  // ---------- 提交回测 ----------
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      ts_code: stockInput.value.trim(),
      buy_price: parseFloat($("#buy_price").value),
      sell_price: parseFloat($("#sell_price").value),
      stop_price: parseFloat($("#stop_price").value),
      lookback_days: parseInt($("#lookback_days").value, 10) || 20,
      initial_capital: parseFloat($("#initial_capital").value) || 100000,
      start_date: $("#start_date").value.trim() || "20170101",
      gain_threshold: parseFloat($("#gain_threshold").value) || 20,
    };

    if (!payload.ts_code || ![payload.buy_price, payload.sell_price, payload.stop_price].every((v) => v > 0)) {
      showError("请完整填写股票代码、买入价、卖出价与止损价。");
      return;
    }
    if (!(payload.buy_price > payload.stop_price)) {
      showError("建议止损价应低于买入价, 请检查输入。");
      return;
    }

    hideError();
    runBtn.disabled = true;
    loading.classList.remove("hidden");
    resultBox.classList.add("hidden");

    try {
      const res = await fetch("/api/backtest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "回测失败");

      renderResult(data);
      loadQuote(data.info.ts_code);
    } catch (err) {
      showError(err.message || "请求失败, 请检查后端服务。");
    } finally {
      loading.classList.add("hidden");
      runBtn.disabled = false;
    }
  });

  // ---------- Tab 切换 ----------
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll('[id^="tab-"]').forEach((p) => {
        p.classList.toggle("hidden", p.id !== `tab-${btn.dataset.tab}`);
      });
      // 切到策略Hub 时刷新策略列表 (自定义策略可能变化)
      if (btn.dataset.tab === "strategies") { loadStrategies(); loadIdeas(); }
      // 切到回测 tab 且 Alpha158 子面板可见时刷新股票池
      if (btn.dataset.tab === "backtest") {
        const ap = document.getElementById("bt-alpha158");
        if (ap && !ap.classList.contains("hidden")) loadAlpha158Pool();
      }
      // 切换后重算图表尺寸 (隐藏容器尺寸为 0)
      setTimeout(() => {
        if (chart) chart.resize();
        if (quoteChart) quoteChart.resize();
        if (bandChart) bandChart.resize();
        if (alphaChart) alphaChart.resize();
        if (alphaDdChart) alphaDdChart.resize();
        if (ptfChart) ptfChart.resize();
      }, 80);
    });
  });

  // ---------- 回测 tab: 买入持有回测 / Alpha158 回测 子面板切换 ----------
  document.querySelectorAll("#seg-backtest .seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#seg-backtest .seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll("#tab-backtest .bt-panel").forEach((p) => {
        p.classList.toggle("hidden", p.dataset.panel !== btn.dataset.panel);
      });
      if (btn.dataset.panel === "alpha158") loadAlpha158Pool();
      if (btn.dataset.panel === "ptf") loadPtfPool();
      // 隐藏面板宽度为 0, 切换后重算图表尺寸
      setTimeout(() => {
        if (chart) chart.resize();
        if (quoteChart) quoteChart.resize();
        if (bandChart) bandChart.resize();
        if (alphaChart) alphaChart.resize();
        if (alphaDdChart) alphaDdChart.resize();
        if (ptfChart) ptfChart.resize();
      }, 80);
    });
  });

  // ---------- 选股 tab: 市场(A股/港股/ETF) × 策略(红利低波/基本面) 切换 ----------
  const screenerState = { mkt: "a", strat: "rlv" };
  // ETF 筛选并入本 tab: 把原 #tab-etf 的内容注入 data-view="etf" 并移除旧 section
  (function moveEtfIntoScreener() {
    const src = document.getElementById("tab-etf");
    const dst = document.querySelector('#tab-screener [data-view="etf"]');
    if (src && dst) {
      while (src.firstChild) dst.appendChild(src.firstChild);
      src.remove();
    }
  })();
  function setScreenerView(mkt, strat) {
    screenerState.mkt = mkt; screenerState.strat = strat;
    document.querySelectorAll("#seg-mkt .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.mkt === mkt));
    document.querySelectorAll("#seg-strat .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.strat === strat));
    const isEtf = mkt === "etf";
    // ETF 模式: 隐藏 红利低波/基本面 策略段 与 顶部存入按钮 (ETF 结果区自带保存按钮)
    document.getElementById("seg-strat")?.classList.toggle("hidden", isEtf);
    document.getElementById("screener-save-btn")?.classList.toggle("hidden", isEtf);
    document.querySelectorAll('#tab-screener [data-view]').forEach((v) => {
      const target = isEtf ? "etf" : mkt + "-" + strat;
      v.classList.toggle("hidden", v.dataset.view !== target);
    });
  }
  document.querySelectorAll("#seg-mkt .seg-btn").forEach((b) =>
    b.addEventListener("click", () => setScreenerView(b.dataset.mkt, screenerState.strat)));
  document.querySelectorAll("#seg-strat .seg-btn").forEach((b) =>
    b.addEventListener("click", () => setScreenerView(screenerState.mkt, b.dataset.strat)));
  // 初始默认 A股 + 红利低波
  setScreenerView("a", "rlv");

  // 把当前选股结果保存到策略Hub (自定义策略)
  function _currentScreenerStocks() {
    const view = document.querySelector('#tab-screener [data-view]:not(.hidden)');
    if (!view) return [];
    const tbody = view.querySelector("table tbody");
    if (!tbody) return [];
    const out = [];
    tbody.querySelectorAll("tr").forEach((tr) => {
      const cells = tr.children;
      if (cells.length < 3) return;
      const nameEl = cells[2].querySelector("a.stock-link");
      const name = nameEl ? nameEl.textContent.trim() : cells[2].textContent.trim();
      const code = cells[1].textContent.trim();
      if (code && code !== "暂无") out.push({ ts_code: code, name });
    });
    return out;
  }
  async function saveScreenerToHub() {
    const stocks = _currentScreenerStocks();
    if (!stocks.length) { alert("当前选股结果为空, 无法保存 (请先在选股 tab 执行一次筛选)"); return; }
    const name = prompt("保存为策略Hub策略, 请输入策略名称:", "");
    if (!name || !name.trim()) return;
    const desc = prompt("策略描述 (可选):", "") || "";
    try {
      const r = await fetch("/api/custom/strategy", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), desc, source: "screener", stocks }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "保存失败");
      alert(`已保存策略「${name.trim()}」, 收录 ${d.added_stocks} 家公司 (可在策略Hub「我的策略」中查看)`);
      loadStrategies();
    } catch (e) {
      alert(e.message || "保存失败");
    }
  }
  const screenerSaveBtn = $("#screener-save-btn");
  if (screenerSaveBtn) screenerSaveBtn.addEventListener("click", saveScreenerToHub);

  // ETF 筛选结果保存到策略Hub (ETF 表: 第1列代码, 第2列名称)
  function _currentEtfStocks() {
    const tbody = document.querySelector("#etf-table tbody");
    if (!tbody) return [];
    const out = [];
    tbody.querySelectorAll("tr").forEach((tr) => {
      const cells = tr.children;
      if (cells.length < 2) return;
      const nameEl = cells[1].querySelector("a.stock-link");
      const name = nameEl ? nameEl.textContent.trim() : cells[1].textContent.trim();
      const code = cells[0].textContent.trim();
      if (code && code !== "暂无") out.push({ ts_code: code, name });
    });
    return out;
  }
  async function saveEtfToHub() {
    const stocks = _currentEtfStocks();
    if (!stocks.length) { alert("当前 ETF 筛选结果为空, 无法保存 (请先执行 ETF 筛选)"); return; }
    const name = prompt("保存为策略Hub策略, 请输入策略名称:", "");
    if (!name || !name.trim()) return;
    const desc = prompt("策略描述 (可选):", "") || "";
    try {
      const r = await fetch("/api/custom/strategy", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), desc, source: "screener", stocks }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "保存失败");
      alert(`已保存策略「${name.trim()}」, 收录 ${d.added_stocks} 只 ETF/公司 (可在策略Hub「我的策略」中查看)`);
      loadStrategies();
    } catch (e) {
      alert(e.message || "保存失败");
    }
  }
  const etfSaveBtn = $("#etf-save-btn");
  if (etfSaveBtn) etfSaveBtn.addEventListener("click", saveEtfToHub);

  // ---------- 选股筛选前端优化: 条件折叠 / 重置 / 筛选徽章(可点击移除) ----------
  // 1) 筛选条件区折叠 (点击 .filter-title 切换其下筛选 grid)
  document.querySelectorAll(".filter-title").forEach((title) => {
    const grid = title.nextElementSibling;
    if (!grid || !grid.classList.contains("grid")) return;
    title.classList.add("ft-toggle");
    title.style.cursor = "pointer";
    const arrow = document.createElement("span");
    arrow.className = "ft-arrow";
    arrow.textContent = "▾";
    title.appendChild(arrow);
    title.addEventListener("click", () => {
      const collapsed = grid.classList.toggle("hidden");
      arrow.textContent = collapsed ? "▸" : "▾";
      title.classList.toggle("collapsed", collapsed);
    });
  });
  // 2) 重置条件按钮: 清空当前表单筛选 grid 的全部输入
  document.querySelectorAll(".filter-reset").forEach((btn) => {
    btn.addEventListener("click", () => {
      const form = btn.closest("form");
      if (!form) return;
      const title = form.querySelector(".filter-title");
      const grid = title ? title.nextElementSibling : null;
      if (grid) grid.querySelectorAll("input").forEach((i) => { i.value = ""; });
    });
  });
  // 3) 筛选徽章: filters 字段 → 输入框映射 (按视图类型)
  const FILTER_BADGE_MAP = {
    rlv: { dividend_yield_ttm: { min: "#rlv_f_dy", max: "#rlv_f_dy_max" }, volatility: { max: "#rlv_f_vol" }, roe: { min: "#rlv_f_roe_min", max: "#rlv_f_roe_max" }, debt_to_assets: { max: "#rlv_f_debt" }, payout_ratio: { min: "#rlv_f_payout_min", max: "#rlv_f_payout_max" }, free_cashflow: { min: "#rlv_f_fcf" }, gross_margin: { min: "#rlv_f_gm" } },
    screen: { roe: { min: "#sc_f_roe", max: "#sc_f_roe_max" }, debt_to_assets: { max: "#sc_f_debt" }, gross_margin: { min: "#sc_f_gm" }, free_cashflow: { min: "#sc_f_fcf" } },
    hk_rlv: { dividend_yield_ttm: { min: "#hk_rlv_f_dy", max: "#hk_rlv_f_dy_max" }, volatility: { max: "#hk_rlv_f_vol" }, roe: { min: "#hk_rlv_f_roe_min", max: "#hk_rlv_f_roe_max" }, debt_to_assets: { max: "#hk_rlv_f_debt" }, payout_ratio: { min: "#hk_rlv_f_payout_min", max: "#hk_rlv_f_payout_max" }, free_cashflow: { min: "#hk_rlv_f_fcf" }, gross_margin: { min: "#hk_rlv_f_gm" } },
    hk_screen: { roe: { min: "#hk_sc_f_roe", max: "#hk_sc_f_roe_max" }, debt_to_assets: { max: "#hk_sc_f_debt" }, gross_margin: { min: "#hk_sc_f_gm" }, free_cashflow: { min: "#hk_sc_f_fcf" } },
  };
  const FILTER_BADGE_LABELS = { dividend_yield_ttm: "股息率TTM", volatility: "波动率", roe: "ROE", debt_to_assets: "资产负债率", payout_ratio: "分红率", gross_margin: "毛利率", free_cashflow: "自由现金流" };
  function filterBadges(filters, view) {
    const map = FILTER_BADGE_MAP[view] || {};
    const badges = [];
    for (const k in (filters || {})) {
      const f = filters[k];
      const m = map[k] || {};
      const lb = FILTER_BADGE_LABELS[k] || k;
      if (f.min !== undefined && f.min !== null && m.min) badges.push(`<span class="filter-badge" data-input="${m.min}">${lb} ≥ ${f.min} ✕</span>`);
      if (f.max !== undefined && f.max !== null && m.max) badges.push(`<span class="filter-badge" data-input="${m.max}">${lb} ≤ ${f.max} ✕</span>`);
    }
    return badges.length ? `<span class="filter-badges">${badges.join("")}</span>` : "";
  }
  // 徽章点击: 清空对应输入并重新触发当前视图筛选
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".filter-badge");
    if (!b) return;
    const inp = document.querySelector(b.dataset.input);
    if (inp) inp.value = "";
    const view = document.querySelector('#tab-screener [data-view]:not(.hidden)');
    const form = view ? view.querySelector("form") : null;
    if (form) form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
  });

  // ---------- 基本面选股 (ROE 杜邦拆分) ----------
  const SCREEN_SORT_LABELS = {
    year: "年份", name: "名称", close: "最近价", roe: "ROE", net_margin: "净利润率",
    assets_turn: "总资产周转率", equity_multiplier: "权益乘数", gross_margin: "毛利率",
    free_cashflow: "自由现金流",
    debt_to_assets: "资产负债率", total_cur_assets: "流动资产", money_cap: "现金",
    invturn_days: "存货周转天数", arturn_days: "应收周转天数",
  };
  const screenSort = { by: "roe", order: "desc" };

  function fmtPctVal(v, digits = 2) {
    if (v === null || v === undefined || v === "") return "—";
    return Number(v).toFixed(digits) + "%";
  }

  function buildScreenPayload() {
    const years = [parseInt($("#sc_year").value, 10)];

    const filters = {};
    const roeMin = parseFloat($("#sc_f_roe").value);
    const roeMax = parseFloat($("#sc_f_roe_max").value);
    if (!isNaN(roeMin) || !isNaN(roeMax)) {
      filters.roe = {};
      if (!isNaN(roeMin)) filters.roe.min = roeMin;
      if (!isNaN(roeMax)) filters.roe.max = roeMax;
    }
    const debt = parseFloat($("#sc_f_debt").value);
    if (!isNaN(debt)) filters.debt_to_assets = { max: debt };
    const gm = parseFloat($("#sc_f_gm").value);
    if (!isNaN(gm)) filters.gross_margin = { min: gm };
    const fcf = parseFloat($("#sc_f_fcf").value);
    if (!isNaN(fcf)) filters.free_cashflow = { min: fcf };

    return {
      industry: $("#sc_industry").value.trim(),
      years,
      sort_by: screenSort.by,
      order: screenSort.order,
      filters,
      max_stocks: 6000,
      limit: 2000,
    };
  }

  function screenFilterLabel(filters) {
    const parts = [];
    for (const k in (filters || {})) {
      const f = filters[k];
      const name = SCREEN_SORT_LABELS[k] || k;
      if (f.min !== undefined && f.min !== null) parts.push(`${name} ≥ ${f.min}`);
      if (f.max !== undefined && f.max !== null) parts.push(`${name} ≤ ${f.max}`);
    }
    return parts.length ? ` · 筛选: ${parts.join(", ")}` : "";
  }

  async function runScreenScreen(payload) {
    screenError.classList.add("hidden");
    screenResult.classList.add("hidden");
    $("#screen-hint").textContent = "";
    screenBtn.disabled = true;
    screenResyncBtn.disabled = true;
    screenLoading.classList.remove("hidden");
    try {
      const res = await fetch("/api/fundamental/screen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "筛选失败");
      renderScreen(data, payload);
    } catch (err) {
      screenError.textContent = err.message || "请求失败, 请检查后端服务。";
      screenError.classList.remove("hidden");
    } finally {
      screenLoading.classList.add("hidden");
      screenBtn.disabled = false;
      screenResyncBtn.disabled = false;
    }
  }

  screenForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const payload = buildScreenPayload();
    if (!payload.years.length) {
      screenError.textContent = "请输入有效年份范围 (如 2024 ~ 2025)。";
      screenError.classList.remove("hidden");
      return;
    }
    runScreenScreen(payload);
  });

  // 表头点击排序
  document.querySelectorAll("#screen-table thead th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const by = th.dataset.sort;
      if (screenSort.by === by) {
        screenSort.order = screenSort.order === "desc" ? "asc" : "desc";
      } else {
        screenSort.by = by;
        screenSort.order = by === "name" ? "asc" : "desc";
      }
      const payload = buildScreenPayload();
      if (!payload.years.length) return;
      runScreenScreen(payload);
    });
  });

  function updateScreenSortArrows() {
    document.querySelectorAll("#screen-table thead th.sortable").forEach((th) => {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      if (th.dataset.sort === screenSort.by) {
        arrow.textContent = screenSort.order === "desc" ? " ▼" : " ▲";
        th.classList.add("sorted");
      } else {
        arrow.textContent = "";
        th.classList.remove("sorted");
      }
    });
  }

  // 强制重新同步 (幂等 upsert)
  screenResyncBtn.addEventListener("click", async () => {
    const payload = buildScreenPayload();
    if (!payload.years.length) {
      screenError.textContent = "请输入有效年份范围。";
      screenError.classList.remove("hidden");
      return;
    }
    screenError.classList.add("hidden");
    $("#screen-hint").textContent = "";
    screenBtn.disabled = true;
    screenResyncBtn.disabled = true;
    screenLoading.classList.remove("hidden");
    try {
      const res = await fetch("/api/fundamental/init", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "同步失败");
      $("#screen-hint").textContent =
        `✓ 强制同步完成: 入库 ${data.stored_total} 条 · ${(data.years || []).join(", ")} 年`;
    } catch (err) {
      screenError.textContent = err.message || "同步失败";
      screenError.classList.remove("hidden");
    } finally {
      screenLoading.classList.add("hidden");
      screenBtn.disabled = false;
      screenResyncBtn.disabled = false;
    }
  });

  function renderScreen(data, payload) {
    const items = data.items || [];
    screenResult.classList.remove("hidden");

    const sortLabel = SCREEN_SORT_LABELS[payload.sort_by] || payload.sort_by;
    const orderLabel = payload.order === "asc" ? "升序" : "降序";
    const synced = data.sync && data.sync.synced ? ` · 本次自动同步入库 ${data.sync.stored} 条` : "";
    const yearsLabel = payload.years.length > 1
      ? `${payload.years[0]}~${payload.years[payload.years.length - 1]}年`
      : `${payload.years[0]}年`;

    $("#screen-title").textContent =
      `基本面筛选 · ${items.length} 条${payload.industry ? ` (${payload.industry})` : " (全市场)"} · ${yearsLabel}`;
    $("#screen-sub").innerHTML =
      `按 ${sortLabel} ${orderLabel}${filterBadges(payload.filters, "screen")} · 数据来自 PostgreSQL${synced}`;

    const body = items.map((it) => `
      <tr>
        <td class="num">${it.year}</td>
        <td>${it.ts_code}</td>
        <td><a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(it.ts_code)}" title="查看详情">${it.name}</a></td>
        <td class="num ${cls(it.roe || 0)}">${fmtPctVal(it.roe)}</td>
        <td class="num ${cls(it.net_margin || 0)}">${fmtPctVal(it.net_margin)}</td>
        <td class="num">${it.assets_turn == null ? "—" : Number(it.assets_turn).toFixed(2)}</td>
        <td class="num">${it.equity_multiplier == null ? "—" : Number(it.equity_multiplier).toFixed(2)}</td>
        <td class="num ${cls(it.gross_margin || 0)}">${fmtPctVal(it.gross_margin)}</td>
        <td class="num">${fmtYi(it.free_cashflow)}</td>
        <td class="num">${fmtPctVal(it.debt_to_assets)}</td>
        <td class="num">${fmtYi(it.total_cur_assets)}</td>
        <td class="num">${fmtYi(it.money_cap)}</td>
        <td class="num">${it.invturn_days == null ? "—" : Number(it.invturn_days).toFixed(1)}</td>
        <td class="num">${it.arturn_days == null ? "—" : Number(it.arturn_days).toFixed(1)}</td>
      </tr>`).join("");
    $("#screen-table tbody").innerHTML = body ||
      `<tr><td colspan="14" style="text-align:center;color:#9ca3af;padding:24px">无符合条件的数据</td></tr>`;
    updateScreenSortArrows();
    screenResult.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------- 红利低波选股 ----------
  const RLV_SORT_LABELS = {
    name: "名称",
    dividend_yield: "静态股息率", dividend_yield_ttm: "股息率TTM", last_close: "上日收盘",
    volatility: "波动率", div_per_share: "每股分红",
    free_cashflow: "自由现金流", gross_margin: "毛利率", eps: "每股收益", payout_ratio: "分红率",
    dividend_growth_3y: "3年股利增长", roe: "ROE", debt_to_assets: "资产负债率",
    avg_daily_mv: "日均市值", avg_daily_amt: "日均成交额",
  };

  // 万元 -> 亿显示
  function fmtYi(v) {
    if (v === null || v === undefined || v === "" || isNaN(v)) return "—";
    const yi = Number(v) / 10000;
    if (Math.abs(yi) >= 100) return yi.toFixed(0) + "亿";
    if (Math.abs(yi) >= 1) return yi.toFixed(2) + "亿";
    return Number(v).toLocaleString("zh-CN") + "万";
  }

  // 当前排序状态 (表头点击修改; 默认股息率降序)
  const rlvSort = { by: "dividend_yield_ttm", order: "desc" };

  // 红利低波年份区间: 起始~结束 (空=最新/不限), 生成年份数组
  function _rlvYears() {
    const yMin = parseInt($("#rlv_year_min").value, 10);
    const yMax = parseInt($("#rlv_year_max").value, 10);
    const cur = new Date().getFullYear();
    if (yMin && yMax && yMax >= yMin) {
      const arr = []; for (let y = yMin; y <= yMax; y++) arr.push(y); return arr;
    }
    if (yMin) {
      const arr = []; for (let y = yMin; y <= cur; y++) arr.push(y); return arr;
    }
    if (yMax) {
      const arr = []; for (let y = 2000; y <= yMax; y++) arr.push(y); return arr;
    }
    return [2025];
  }

  // 从表单构造红利低波请求 (多年份数组 + 筛选条件 + 当前排序)
  function buildRlvPayload() {
    const years = _rlvYears();

    const filters = {};
    const fNum = (id) => { const v = parseFloat($(id).value); return isNaN(v) ? null : v; };
    const dyMin = fNum("#rlv_f_dy");
    const dyMax = fNum("#rlv_f_dy_max");
    if (dyMin !== null || dyMax !== null) {
      filters.dividend_yield_ttm = {};
      if (dyMin !== null) filters.dividend_yield_ttm.min = dyMin;
      if (dyMax !== null) filters.dividend_yield_ttm.max = dyMax;
    }
    const vol = fNum("#rlv_f_vol"); if (vol !== null) filters.volatility = { max: vol };
    const roeMin = fNum("#rlv_f_roe_min");
    const roeMax = fNum("#rlv_f_roe_max");
    if (roeMin !== null || roeMax !== null) {
      filters.roe = {};
      if (roeMin !== null) filters.roe.min = roeMin;
      if (roeMax !== null) filters.roe.max = roeMax;
    }
    const debt = fNum("#rlv_f_debt"); if (debt !== null) filters.debt_to_assets = { max: debt };
    const payoutMin = fNum("#rlv_f_payout_min");
    const payoutMax = fNum("#rlv_f_payout_max");
    if (payoutMin !== null || payoutMax !== null) {
      filters.payout_ratio = {};
      if (payoutMin !== null) filters.payout_ratio.min = payoutMin;
      if (payoutMax !== null) filters.payout_ratio.max = payoutMax;
    }
    const fcf = fNum("#rlv_f_fcf"); if (fcf !== null) filters.free_cashflow = { min: fcf };
    const gm = fNum("#rlv_f_gm"); if (gm !== null) filters.gross_margin = { min: gm };

    return {
      industry: $("#rlv_industry").value.trim(),
      years,
      sort_by: rlvSort.by,
      order: rlvSort.order,
      filters,
      max_stocks: 6000,
      limit: 1000,
    };
  }

  function filterLabel(filters) {
    const labels = RLV_SORT_LABELS;
    const parts = [];
    for (const k in (filters || {})) {
      const f = filters[k];
      const name = labels[k] || k;
      if (f.min !== undefined && f.min !== null && f.min !== "") parts.push(`${name} ≥ ${f.min}`);
      if (f.max !== undefined && f.max !== null && f.max !== "") parts.push(`${name} ≤ ${f.max}`);
    }
    return parts.length ? ` · 筛选: ${parts.join(", ")}` : "";
  }

  async function runRlvScreen(payload) {
    rlvError.classList.add("hidden");
    rlvResult.classList.add("hidden");
    $("#rlv-hint").textContent = "";
    rlvBtn.disabled = true;
    rlvResyncBtn.disabled = true;
    rlvLoading.classList.remove("hidden");
    try {
      const res = await fetch("/api/redlowvol/screen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "选股失败");
      renderRlv(data, payload);
    } catch (err) {
      rlvError.textContent = err.message || "请求失败, 请检查后端服务。";
      rlvError.classList.remove("hidden");
    } finally {
      rlvLoading.classList.add("hidden");
      rlvBtn.disabled = false;
      rlvResyncBtn.disabled = false;
    }
  }

  rlvForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const payload = buildRlvPayload();
    if (!payload.years.length) {
      rlvError.textContent = "请输入有效年份范围 (如 2020 ~ 2025)。";
      rlvError.classList.remove("hidden");
      return;
    }
    runRlvScreen(payload);
  });

  // 表头点击排序 (无需手动选择排序指标)
  document.querySelectorAll("#rlv-table thead th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const by = th.dataset.sort;
      if (rlvSort.by === by) {
        rlvSort.order = rlvSort.order === "desc" ? "asc" : "desc";
      } else {
        rlvSort.by = by;
        // 名称等文本列首次点击默认升序 (更符合排序直觉)
        rlvSort.order = by === "name" ? "asc" : "desc";
      }
      const payload = buildRlvPayload();
      if (!payload.years.length) return;
      runRlvScreen(payload);
    });
  });

  function updateRlvSortArrows() {
    document.querySelectorAll("#rlv-table thead th.sortable").forEach((th) => {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      if (th.dataset.sort === rlvSort.by) {
        arrow.textContent = rlvSort.order === "desc" ? " ▼" : " ▲";
        th.classList.add("sorted");
      } else {
        arrow.textContent = "";
        th.classList.remove("sorted");
      }
    });
  }

  // 强制重新同步 (幂等 upsert)
  rlvResyncBtn.addEventListener("click", async () => {
    const payload = buildRlvPayload();
    if (!payload.years.length) {
      rlvError.textContent = "请输入有效年份范围。";
      rlvError.classList.remove("hidden");
      return;
    }
    rlvError.classList.add("hidden");
    $("#rlv-hint").textContent = "";
    rlvBtn.disabled = true;
    rlvResyncBtn.disabled = true;
    rlvLoading.classList.remove("hidden");
    try {
      const res = await fetch("/api/redlowvol/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "同步失败");
      $("#rlv-hint").textContent =
        `✓ 强制同步完成: 入库 ${data.stored_total} 条 · ${(data.years || []).join(", ")} 年`;
    } catch (err) {
      rlvError.textContent = err.message || "同步失败";
      rlvError.classList.remove("hidden");
    } finally {
      rlvLoading.classList.add("hidden");
      rlvBtn.disabled = false;
      rlvResyncBtn.disabled = false;
    }
  });

  function renderRlv(data, payload) {
    const items = data.items || [];
    rlvResult.classList.remove("hidden");

    const sortLabel = RLV_SORT_LABELS[payload.sort_by] || payload.sort_by;
    const orderLabel = payload.order === "asc" ? "升序" : "降序";
    const synced = data.sync && data.sync.synced ? ` · 本次自动同步入库 ${data.sync.stored} 条` : "";
    const yearsLabel = payload.years.length > 1
      ? `${payload.years[0]}~${payload.years[payload.years.length - 1]}年`
      : `${payload.years[0]}年`;

    $("#rlv-title").textContent =
      `红利低波排序 · ${items.length} 条${payload.industry ? ` (${payload.industry})` : " (全市场)"} · ${yearsLabel}`;
    $("#rlv-sub").innerHTML =
      `按 ${sortLabel} ${orderLabel}${filterBadges(payload.filters, "rlv")} · 数据来自 PostgreSQL${synced}`;

    const body = items.map((it) => `
      <tr>
        <td class="num">${it.year}</td>
        <td>${it.ts_code}</td>
        <td><a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(it.ts_code)}" title="查看详情">${it.name}</a></td>
        <td>${it.industry || "—"}</td>
        <td class="num ${cls(it.dividend_yield || 0)}">${fmtPctVal(it.dividend_yield)}</td>
        <td class="num ${cls(it.dividend_yield_ttm || 0)}">${fmtPctVal(it.dividend_yield_ttm)}</td>
        <td class="num">${it.last_close == null ? "—" : Number(it.last_close).toFixed(2)}</td>
        <td class="num">${fmtPctVal(it.volatility)}</td>
        <td class="num">${it.div_per_share == null ? "—" : Number(it.div_per_share).toFixed(2) + " 元"}</td>
        <td class="num">${fmtYi(it.free_cashflow)}</td>
        <td class="num ${cls(it.gross_margin || 0)}">${fmtPctVal(it.gross_margin)}</td>
        <td class="num">${it.eps == null ? "—" : Number(it.eps).toFixed(2)}</td>
        <td class="num">${fmtPctVal(it.payout_ratio)}</td>
        <td class="num ${cls(it.roe || 0)}">${fmtPctVal(it.roe)}</td>
        <td class="num">${fmtPctVal(it.debt_to_assets)}</td>
      </tr>`).join("");
    $("#rlv-table tbody").innerHTML = body ||
      `<tr><td colspan="14" style="text-align:center;color:#9ca3af;padding:24px">无符合条件的数据</td></tr>`;
    updateRlvSortArrows();
    rlvResult.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------- 港股选股 (红利低波 / 基本面) ----------
  fillYearSelect($("#hk_sc_year"), 2025);

  // 港股行业联想 (使用 /api/hk/industry/search)
  function bindHkIndustrySuggest(input, panel) {
    let timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const kw = input.value.trim();
      if (!kw) { panel.innerHTML = ""; panel.classList.add("hidden"); return; }
      timer = setTimeout(async () => {
        try {
          const r = await fetch(`/api/hk/industry/search?keyword=${encodeURIComponent(kw)}&limit=12`);
          const data = await r.json();
          const items = data.items || [];
          panel.innerHTML = items.length
            ? items.map((it) =>
                `<div class="ind-item" data-ind="${it.industry}"><span>${it.industry}</span><span class="ind-count">${it.count} 只</span></div>`).join("")
            : `<div class="ind-empty">无匹配行业</div>`;
          panel.classList.remove("hidden");
        } catch (e) { /* 忽略联想失败 */ }
      }, 200);
    });
    panel.addEventListener("click", (e) => {
      const item = e.target.closest(".ind-item");
      if (!item) return;
      input.value = item.dataset.ind;
      panel.innerHTML = "";
      panel.classList.add("hidden");
      input.dispatchEvent(new Event("change"));
    });
    document.addEventListener("click", (e) => {
      if (!input.contains(e.target) && !panel.contains(e.target)) {
        panel.innerHTML = "";
        panel.classList.add("hidden");
      }
    });
    input.addEventListener("blur", () => {
      setTimeout(() => {
        if (!panel.contains(document.activeElement)) {
          panel.classList.add("hidden");
        }
      }, 150);
    });
  }
  bindHkIndustrySuggest($("#hk_rlv_industry"), $("#hk-rlv-ind-panel"));
  bindHkIndustrySuggest($("#hk_sc_industry"), $("#hk-sc-ind-panel"));

  // ---------- 港股红利低波 ----------
  const HK_RLV_SORT_LABELS = {
    name: "名称", dividend_yield: "静态股息率", dividend_yield_ttm: "股息率TTM",
    last_close: "上日收盘", volatility: "波动率", div_per_share: "每股分红",
    free_cashflow: "自由现金流", gross_margin: "毛利率", eps: "每股收益", payout_ratio: "分红率",
    dividend_growth_3y: "3年股利增长", roe: "ROE", debt_to_assets: "资产负债率",
    avg_daily_mv: "总市值", avg_daily_amt: "日均成交额",
  };
  const hkRlvSort = { by: "dividend_yield_ttm", order: "desc" };

  function _hkRlvYears() {
    const yMin = parseInt($("#hk_rlv_year_min").value, 10);
    const yMax = parseInt($("#hk_rlv_year_max").value, 10);
    const cur = new Date().getFullYear();
    if (yMin && yMax && yMax >= yMin) {
      const arr = []; for (let y = yMin; y <= yMax; y++) arr.push(y); return arr;
    }
    if (yMin) { const arr = []; for (let y = yMin; y <= cur; y++) arr.push(y); return arr; }
    if (yMax) { const arr = []; for (let y = 2000; y <= yMax; y++) arr.push(y); return arr; }
    return [2025];
  }

  function buildHkRlvPayload() {
    const years = _hkRlvYears();
    const filters = {};
    const fNum = (id) => { const v = parseFloat($(id).value); return isNaN(v) ? null : v; };
    const dyMin = fNum("#hk_rlv_f_dy");
    const dyMax = fNum("#hk_rlv_f_dy_max");
    if (dyMin !== null || dyMax !== null) {
      filters.dividend_yield_ttm = {};
      if (dyMin !== null) filters.dividend_yield_ttm.min = dyMin;
      if (dyMax !== null) filters.dividend_yield_ttm.max = dyMax;
    }
    const vol = fNum("#hk_rlv_f_vol"); if (vol !== null) filters.volatility = { max: vol };
    const roeMin = fNum("#hk_rlv_f_roe_min");
    const roeMax = fNum("#hk_rlv_f_roe_max");
    if (roeMin !== null || roeMax !== null) {
      filters.roe = {};
      if (roeMin !== null) filters.roe.min = roeMin;
      if (roeMax !== null) filters.roe.max = roeMax;
    }
    const debt = fNum("#hk_rlv_f_debt"); if (debt !== null) filters.debt_to_assets = { max: debt };
    const payoutMin = fNum("#hk_rlv_f_payout_min");
    const payoutMax = fNum("#hk_rlv_f_payout_max");
    if (payoutMin !== null || payoutMax !== null) {
      filters.payout_ratio = {};
      if (payoutMin !== null) filters.payout_ratio.min = payoutMin;
      if (payoutMax !== null) filters.payout_ratio.max = payoutMax;
    }
    const fcf = fNum("#hk_rlv_f_fcf"); if (fcf !== null) filters.free_cashflow = { min: fcf };
    const gm = fNum("#hk_rlv_f_gm"); if (gm !== null) filters.gross_margin = { min: gm };
    return {
      industry: $("#hk_rlv_industry").value.trim(),
      years, sort_by: hkRlvSort.by, order: hkRlvSort.order,
      filters, max_stocks: 3000, limit: 1000,
    };
  }

  function hkFilterLabel(filters) {
    const parts = [];
    for (const k in (filters || {})) {
      const f = filters[k];
      const name = HK_RLV_SORT_LABELS[k] || k;
      if (f.min !== undefined && f.min !== null && f.min !== "") parts.push(`${name} ≥ ${f.min}`);
      if (f.max !== undefined && f.max !== null && f.max !== "") parts.push(`${name} ≤ ${f.max}`);
    }
    return parts.length ? ` · 筛选: ${parts.join(", ")}` : "";
  }

  async function runHkRlvScreen(payload) {
    const errBox = $("#hk-rlv-error"), resBox = $("#hk-rlv-result");
    errBox.classList.add("hidden");
    resBox.classList.add("hidden");
    $("#hk-rlv-hint").textContent = "";
    $("#hk-rlv-btn").disabled = true;
    $("#hk-rlv-resync-btn").disabled = true;
    $("#hk-rlv-loading").classList.remove("hidden");
    try {
      const res = await fetch("/api/hk/redlowvol/screen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "选股失败");
      renderHkRlv(data, payload);
    } catch (err) {
      errBox.textContent = err.message || "请求失败, 请检查后端服务。";
      errBox.classList.remove("hidden");
    } finally {
      $("#hk-rlv-loading").classList.add("hidden");
      $("#hk-rlv-btn").disabled = false;
      $("#hk-rlv-resync-btn").disabled = false;
    }
  }

  $("#hk-rlv-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const payload = buildHkRlvPayload();
    if (!payload.years.length) {
      $("#hk-rlv-error").textContent = "请输入有效年份范围。";
      $("#hk-rlv-error").classList.remove("hidden");
      return;
    }
    runHkRlvScreen(payload);
  });

  document.querySelectorAll("#hk-rlv-table thead th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const by = th.dataset.sort;
      if (hkRlvSort.by === by) {
        hkRlvSort.order = hkRlvSort.order === "desc" ? "asc" : "desc";
      } else {
        hkRlvSort.by = by;
        hkRlvSort.order = by === "name" ? "asc" : "desc";
      }
      const payload = buildHkRlvPayload();
      if (!payload.years.length) return;
      runHkRlvScreen(payload);
    });
  });

  function updateHkRlvSortArrows() {
    document.querySelectorAll("#hk-rlv-table thead th.sortable").forEach((th) => {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      if (th.dataset.sort === hkRlvSort.by) {
        arrow.textContent = hkRlvSort.order === "desc" ? " ▼" : " ▲";
        th.classList.add("sorted");
      } else {
        arrow.textContent = "";
        th.classList.remove("sorted");
      }
    });
  }

  $("#hk-rlv-resync-btn").addEventListener("click", async () => {
    const payload = buildHkRlvPayload();
    if (!payload.years.length) {
      $("#hk-rlv-error").textContent = "请输入有效年份范围。";
      $("#hk-rlv-error").classList.remove("hidden");
      return;
    }
    $("#hk-rlv-error").classList.add("hidden");
    $("#hk-rlv-hint").textContent = "";
    $("#hk-rlv-btn").disabled = true;
    $("#hk-rlv-resync-btn").disabled = true;
    $("#hk-rlv-loading").classList.remove("hidden");
    try {
      const res = await fetch("/api/hk/redlowvol/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "同步失败");
      $("#hk-rlv-hint").textContent =
        `✓ 强制同步完成: 入库 ${data.stored_total} 条 · ${(data.years || []).join(", ")} 年`;
    } catch (err) {
      $("#hk-rlv-error").textContent = err.message || "同步失败";
      $("#hk-rlv-error").classList.remove("hidden");
    } finally {
      $("#hk-rlv-loading").classList.add("hidden");
      $("#hk-rlv-btn").disabled = false;
      $("#hk-rlv-resync-btn").disabled = false;
    }
  });

  function renderHkRlv(data, payload) {
    const items = data.items || [];
    const resBox = $("#hk-rlv-result");
    resBox.classList.remove("hidden");

    const sortLabel = HK_RLV_SORT_LABELS[payload.sort_by] || payload.sort_by;
    const orderLabel = payload.order === "asc" ? "升序" : "降序";
    const synced = data.sync && data.sync.synced ? ` · 本次自动同步入库 ${data.sync.stored} 条` : "";
    const yearsLabel = payload.years.length > 1
      ? `${payload.years[0]}~${payload.years[payload.years.length - 1]}年`
      : `${payload.years[0]}年`;

    $("#hk-rlv-title").textContent =
      `港股红利低波排序 · ${items.length} 条${payload.industry ? ` (${payload.industry})` : " (全市场)"} · ${yearsLabel}`;
    $("#hk-rlv-sub").innerHTML =
      `按 ${sortLabel} ${orderLabel}${filterBadges(payload.filters, "hk_rlv")} · 数据来自 PostgreSQL${synced}`;

    const body = items.map((it) => `
      <tr>
        <td class="num">${it.year}</td>
        <td>${it.ts_code}</td>
        <td class="stock-name"><a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(it.ts_code)}" title="查看详情">${it.name}</a></td>
        <td>${it.industry || "—"}</td>
        <td>${it.market || "—"}</td>
        <td class="num ${cls(it.dividend_yield || 0)}">${fmtPctVal(it.dividend_yield)}</td>
        <td class="num ${cls(it.dividend_yield_ttm || 0)}">${fmtPctVal(it.dividend_yield_ttm)}</td>
        <td class="num">${it.last_close == null ? "—" : Number(it.last_close).toFixed(2)}</td>
        <td class="num">${fmtPctVal(it.volatility)}</td>
        <td class="num">${it.div_per_share == null ? "—" : Number(it.div_per_share).toFixed(2) + " 元"}</td>
        <td class="num">${fmtYi(it.free_cashflow)}</td>
        <td class="num ${cls(it.gross_margin || 0)}">${fmtPctVal(it.gross_margin)}</td>
        <td class="num">${it.eps == null ? "—" : Number(it.eps).toFixed(2)}</td>
        <td class="num">${fmtPctVal(it.payout_ratio)}</td>
        <td class="num ${cls(it.roe || 0)}">${fmtPctVal(it.roe)}</td>
        <td class="num">${fmtPctVal(it.debt_to_assets)}</td>
      </tr>`).join("");
    $("#hk-rlv-table tbody").innerHTML = body ||
      `<tr><td colspan="15" style="text-align:center;color:#9ca3af;padding:24px">无符合条件的数据</td></tr>`;
    updateHkRlvSortArrows();
    resBox.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------- 港股基本面 ----------
  const HK_SCREEN_SORT_LABELS = {
    year: "年份", name: "名称", close: "最近价", roe: "ROE", net_margin: "净利润率",
    assets_turn: "总资产周转率", equity_multiplier: "权益乘数", gross_margin: "毛利率",
    free_cashflow: "自由现金流",
    debt_to_assets: "资产负债率", current_ratio: "流动比率", total_cur_assets: "流动资产",
    money_cap: "现金", invturn_days: "存货周转天数", arturn_days: "应收周转天数",
    eps: "每股收益", operate_income: "营业收入", net_profit: "净利润", total_mv: "总市值",
  };
  const hkScreenSort = { by: "roe", order: "desc" };

  function buildHkScreenPayload() {
    const years = [parseInt($("#hk_sc_year").value, 10)];
    const filters = {};
    const roeMin = parseFloat($("#hk_sc_f_roe").value);
    const roeMax = parseFloat($("#hk_sc_f_roe_max").value);
    if (!isNaN(roeMin) || !isNaN(roeMax)) {
      filters.roe = {};
      if (!isNaN(roeMin)) filters.roe.min = roeMin;
      if (!isNaN(roeMax)) filters.roe.max = roeMax;
    }
    const debt = parseFloat($("#hk_sc_f_debt").value);
    if (!isNaN(debt)) filters.debt_to_assets = { max: debt };
    const gm = parseFloat($("#hk_sc_f_gm").value);
    if (!isNaN(gm)) filters.gross_margin = { min: gm };
    const fcf = parseFloat($("#hk_sc_f_fcf").value);
    if (!isNaN(fcf)) filters.free_cashflow = { min: fcf };
    return {
      industry: $("#hk_sc_industry").value.trim(),
      years, sort_by: hkScreenSort.by, order: hkScreenSort.order,
      filters, max_stocks: 3000, limit: 2000,
    };
  }

  async function runHkScreenScreen(payload) {
    const errBox = $("#hk-screen-error"), resBox = $("#hk-screen-result");
    errBox.classList.add("hidden");
    resBox.classList.add("hidden");
    $("#hk-screen-hint").textContent = "";
    $("#hk-screen-btn").disabled = true;
    $("#hk-screen-resync-btn").disabled = true;
    $("#hk-screen-loading").classList.remove("hidden");
    try {
      const res = await fetch("/api/hk/fundamental/screen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "筛选失败");
      renderHkScreen(data, payload);
    } catch (err) {
      errBox.textContent = err.message || "请求失败, 请检查后端服务。";
      errBox.classList.remove("hidden");
    } finally {
      $("#hk-screen-loading").classList.add("hidden");
      $("#hk-screen-btn").disabled = false;
      $("#hk-screen-resync-btn").disabled = false;
    }
  }

  $("#hk-screen-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const payload = buildHkScreenPayload();
    if (!payload.years.length) {
      $("#hk-screen-error").textContent = "请输入有效年份。";
      $("#hk-screen-error").classList.remove("hidden");
      return;
    }
    runHkScreenScreen(payload);
  });

  document.querySelectorAll("#hk-screen-table thead th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const by = th.dataset.sort;
      if (hkScreenSort.by === by) {
        hkScreenSort.order = hkScreenSort.order === "desc" ? "asc" : "desc";
      } else {
        hkScreenSort.by = by;
        hkScreenSort.order = by === "name" ? "asc" : "desc";
      }
      const payload = buildHkScreenPayload();
      if (!payload.years.length) return;
      runHkScreenScreen(payload);
    });
  });

  function updateHkScreenSortArrows() {
    document.querySelectorAll("#hk-screen-table thead th.sortable").forEach((th) => {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      if (th.dataset.sort === hkScreenSort.by) {
        arrow.textContent = hkScreenSort.order === "desc" ? " ▼" : " ▲";
        th.classList.add("sorted");
      } else {
        arrow.textContent = "";
        th.classList.remove("sorted");
      }
    });
  }

  $("#hk-screen-resync-btn").addEventListener("click", async () => {
    const payload = buildHkScreenPayload();
    if (!payload.years.length) {
      $("#hk-screen-error").textContent = "请输入有效年份。";
      $("#hk-screen-error").classList.remove("hidden");
      return;
    }
    $("#hk-screen-error").classList.add("hidden");
    $("#hk-screen-hint").textContent = "";
    $("#hk-screen-btn").disabled = true;
    $("#hk-screen-resync-btn").disabled = true;
    $("#hk-screen-loading").classList.remove("hidden");
    try {
      const res = await fetch("/api/hk/fundamental/init", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "同步失败");
      $("#hk-screen-hint").textContent =
        `✓ 强制同步完成: 入库 ${data.stored_total} 条 · ${(data.years || []).join(", ")} 年`;
    } catch (err) {
      $("#hk-screen-error").textContent = err.message || "同步失败";
      $("#hk-screen-error").classList.remove("hidden");
    } finally {
      $("#hk-screen-loading").classList.add("hidden");
      $("#hk-screen-btn").disabled = false;
      $("#hk-screen-resync-btn").disabled = false;
    }
  });

  function renderHkScreen(data, payload) {
    const items = data.items || [];
    const resBox = $("#hk-screen-result");
    resBox.classList.remove("hidden");

    const sortLabel = HK_SCREEN_SORT_LABELS[payload.sort_by] || payload.sort_by;
    const orderLabel = payload.order === "asc" ? "升序" : "降序";
    const synced = data.sync && data.sync.synced ? ` · 本次自动同步入库 ${data.sync.stored} 条` : "";
    const yearsLabel = payload.years.length > 1
      ? `${payload.years[0]}~${payload.years[payload.years.length - 1]}年`
      : `${payload.years[0]}年`;

    $("#hk-screen-title").textContent =
      `港股基本面筛选 · ${items.length} 条${payload.industry ? ` (${payload.industry})` : " (全市场)"} · ${yearsLabel}`;
    $("#hk-screen-sub").innerHTML =
      `按 ${sortLabel} ${orderLabel}${filterBadges(payload.filters, "hk_screen")} · 数据来自 PostgreSQL${synced}`;

    const body = items.map((it) => `
      <tr>
        <td class="num">${it.year}</td>
        <td>${it.ts_code}</td>
        <td class="stock-name"><a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(it.ts_code)}" title="查看详情">${it.name}</a></td>
        <td>${it.industry || "—"}</td>
        <td class="num ${cls(it.roe || 0)}">${fmtPctVal(it.roe)}</td>
        <td class="num ${cls(it.net_margin || 0)}">${fmtPctVal(it.net_margin)}</td>
        <td class="num">${it.assets_turn == null ? "—" : Number(it.assets_turn).toFixed(2)}</td>
        <td class="num">${it.equity_multiplier == null ? "—" : Number(it.equity_multiplier).toFixed(2)}</td>
        <td class="num ${cls(it.gross_margin || 0)}">${fmtPctVal(it.gross_margin)}</td>
        <td class="num">${fmtYi(it.free_cashflow)}</td>
        <td class="num">${fmtPctVal(it.debt_to_assets)}</td>
        <td class="num">${it.current_ratio == null ? "—" : Number(it.current_ratio).toFixed(2)}</td>
        <td class="num">${fmtYi(it.total_cur_assets)}</td>
        <td class="num">${fmtYi(it.money_cap)}</td>
        <td class="num">${it.invturn_days == null ? "—" : Number(it.invturn_days).toFixed(1)}</td>
        <td class="num">${it.arturn_days == null ? "—" : Number(it.arturn_days).toFixed(1)}</td>
      </tr>`).join("");
    $("#hk-screen-table tbody").innerHTML = body ||
      `<tr><td colspan="15" style="text-align:center;color:#9ca3af;padding:24px">无符合条件的数据</td></tr>`;
    updateHkScreenSortArrows();
    resBox.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------- 最近行情 K 线 ----------
  function ma(arr, n) {
    const out = [];
    for (let i = 0; i < arr.length; i++) {
      if (i < n - 1) { out.push("-"); continue; }
      let sum = 0;
      for (let j = i - n + 1; j <= i; j++) sum += arr[j];
      out.push(+(sum / n).toFixed(3));
    }
    return out;
  }

  async function loadQuote(tsCode) {
    const section = $("#quote-section");
    try {
      const r = await fetch(`/api/quote/${encodeURIComponent(tsCode)}?days=120`);
      if (!r.ok) throw new Error();
      const data = await r.json();
      if (!data.bars || !data.bars.length) throw new Error();
      // 先显示容器, 再初始化图表, 确保 ECharts 拿到正确的宽高
      section.classList.remove("hidden");
      renderQuote(data);
    } catch (e) {
      hideQuote();
    }
  }

  function hideQuote() {
    const section = $("#quote-section");
    if (section) section.classList.add("hidden");
  }

  function renderQuote(data) {
    const el = $("#quote-chart");
    if (!quoteChart) quoteChart = echarts.init(el);

    const bars = data.bars;
    const dates = bars.map((b) => fmtDate(b.date));
    const kData = bars.map((b) => [b.open, b.close, b.low, b.high]);
    const closes = bars.map((b) => b.close);
    const vols = bars.map((b) => b.vol);
    const volColors = bars.map((b) =>
      b.close >= b.open ? "rgba(230, 80, 80, .7)" : "rgba(38, 166, 154, .7)");

    $("#quote-title").textContent = `最近行情 · ${data.info.name} (${data.info.ts_code})`;
    $("#quote-sub").textContent =
      `${fmtDate(data.start)} ~ ${fmtDate(data.end)} · ${data.days} 个交易日` +
      ` · 最新收盘 ${closes[closes.length - 1].toFixed(3)}` +
      ` · ${data.info.kind === "fund" ? "基金/ETF" : "股票"}`;

    quoteChart.setOption({
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (params) => {
          const i = params[0].dataIndex;
          const b = bars[i];
          let html = `<b>${fmtDate(b.date)}</b><br/>`;
          html += `开盘 ${b.open.toFixed(3)}　收盘 <b>${b.close.toFixed(3)}</b><br/>`;
          html += `最高 ${b.high.toFixed(3)}　最低 ${b.low.toFixed(3)}<br/>`;
          html += `涨跌 <b style="color:${b.pct_chg >= 0 ? "#e65050" : "#26a69a"}">${b.pct_chg >= 0 ? "+" : ""}${b.pct_chg.toFixed(2)}%</b>`;
          return html;
        },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      legend: { top: 4, data: ["K线", "MA5", "MA20", "成交量"], textStyle: { fontSize: 12 } },
      grid: [
        { left: 62, right: 20, top: 36, height: "54%" },
        { left: 62, right: 20, top: "74%", height: "14%" },
      ],
      xAxis: [
        { type: "category", data: dates, boundaryGap: true,
          axisLine: { lineStyle: { color: "#e5e7eb" } },
          axisLabel: { color: "#9ca3af", fontSize: 10, hideOverlap: true,
                       formatter: (v) => v.slice(5) } },
        { type: "category", gridIndex: 1, data: dates, boundaryGap: true,
          axisLabel: { show: false }, axisLine: { lineStyle: { color: "#e5e7eb" } } },
      ],
      yAxis: [
        { scale: true, axisLabel: { color: "#9ca3af", fontSize: 10 },
          splitLine: { lineStyle: { color: "#f1f5f9" } } },
        { gridIndex: 1, splitNumber: 2, axisLabel: { show: false },
          splitLine: { show: false } },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: 0, end: 100,
          zoomOnMouseWheel: true, moveOnMouseMove: true },
        { type: "slider", xAxisIndex: [0, 1], top: "93%", height: 18,
          start: 0, end: 100, showDetail: true },
      ],
      series: [
        {
          name: "K线", type: "candlestick", data: kData,
          itemStyle: { color: "#e65050", color0: "#26a69a",
                       borderColor: "#e65050", borderColor0: "#26a69a" },
        },
        { name: "MA5", type: "line", data: ma(closes, 5), smooth: true,
          symbol: "none", lineStyle: { width: 1, color: "#f59e0b" } },
        { name: "MA20", type: "line", data: ma(closes, 20), smooth: true,
          symbol: "none", lineStyle: { width: 1, color: "#3b82f6" } },
        { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
          data: vols, itemStyle: { color: (p) => volColors[p.dataIndex] || "#e65050" } },
      ],
    }, true);
    // 容器刚由隐藏转为可见, 下一帧重算尺寸, 保证自适应
    requestAnimationFrame(() => quoteChart && quoteChart.resize());
  }

  // ---------- 渲染 ----------
  function renderResult(data) {
    const { info, params, range, strategies } = data;
    resultBox.classList.remove("hidden");

    $("#result-title").textContent = `${info.name} (${info.ts_code}) · 四策略回测`;
    $("#result-sub").textContent =
      `${fmtDate(range.start)} ~ ${fmtDate(range.end)} · ${range.bars} 个交易日 · ` +
      `参考价区间 ${range.first_close.toFixed(2)} ~ ${range.last_close.toFixed(2)} · ` +
      `买入 ${params.buy_price} / 卖出 ${params.sell_price} / 止损 ${params.stop_price}`;
    $("#range-hint").textContent =
      `数据区间 ${fmtDate(range.start)} ~ ${fmtDate(range.end)} (${range.bars} 根)`;

    renderMetrics(data);
    renderTable(data);
    renderChart(data);
    resultBox.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderMetrics(data) {
    const { params } = data;
    const strategies = data.strategies;
    const accents = ["accent1", "accent2", "accent3"];
    let cards = strategies.map((s, i) => {
      const m = s.metrics;
      return `
        <div class="metric ${accents[i % 3]}">
          <div class="m-label">${s.name} · 总收益率</div>
          <div class="m-value ${cls(m.total_return)}">${fmtPct(m.total_return)}</div>
          <div class="m-sub">最大回撤 ${m.max_drawdown.toFixed(2)}% · 卡玛 ${m.calmar.toFixed(2)} · 夏普 ${m.sharpe.toFixed(2)}</div>
        </div>`;
    }).join("");

    const best = strategies.reduce((a, b) =>
      (b.metrics.total_return > a.metrics.total_return ? b : a));
    const worst = strategies.reduce((a, b) =>
      (b.metrics.max_drawdown > a.metrics.max_drawdown ? b : a));

    cards += `
      <div class="metric up">
        <div class="m-label">最优策略</div>
        <div class="m-value">${best.name}</div>
        <div class="m-sub">总收益率 ${fmtPct(best.metrics.total_return)}</div>
      </div>
      <div class="metric down">
        <div class="m-label">最大回撤策略</div>
        <div class="m-value">${worst.name}</div>
        <div class="m-sub">回撤 ${worst.metrics.max_drawdown.toFixed(2)}%</div>
      </div>`;

    $("#metric-cards").innerHTML = cards;
  }

  function renderTable(data) {
    const tags = { "买入持有": "bh", "限价买入持有": "lbh", "区间交易": "band", "低价买入": "low" };
    const body = data.strategies.map((s) => {
      const m = s.metrics;
      return `
        <tr>
          <td><span class="tag ${tags[s.name] || "bh"}">${s.name}</span></td>
          <td class="num ${cls(m.total_return)}">${fmtPct(m.total_return)}</td>
          <td class="num ${cls(m.annual_return)}">${fmtPct(m.annual_return)}</td>
          <td class="num ${cls(-m.max_drawdown)}">${m.max_drawdown.toFixed(2)}%</td>
          <td class="num">${m.calmar.toFixed(2)}</td>
          <td class="num">${m.sharpe.toFixed(2)}</td>
          <td class="num">¥ ${fmtNum(m.final_value)}</td>
        </tr>`;
    }).join("");
    $("#metrics-table tbody").innerHTML = body;
  }

  function renderChart(data) {
    const el = $("#chart");
    if (!chart) chart = echarts.init(el);
    const colors = ["#4f46e5", "#0891b2", "#ea580c", "#059669"];
    const series = data.strategies.map((s, i) => ({
      name: s.name,
      type: "line",
      data: s.returns_pct,
      showSymbol: false,
      smooth: true,
      lineStyle: { width: 2.2 },
      itemStyle: { color: colors[i] },
      areaStyle: { opacity: 0.06 },
    }));

    const dates = data.strategies[0].dates.map(fmtDate);
    chart.setOption({
      animationDuration: 500,
      tooltip: {
        trigger: "axis",
        formatter: (params) => {
          let html = `<b>${params[0].axisValue}</b><br/>`;
          params.forEach((p) => {
            html += `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${p.color};margin-right:6px"></span>${p.seriesName}: <b>${p.value >= 0 ? "+" : ""}${p.value.toFixed(2)}%</b><br/>`;
          });
          return html;
        },
      },
      legend: { top: 4, textStyle: { fontSize: 13 } },
      grid: { left: 14, right: 20, top: 44, bottom: 36, containLabel: true },
      xAxis: {
        type: "category",
        data: dates,
        boundaryGap: false,
        axisLabel: { color: "#6b7280", fontSize: 11 },
        axisLine: { lineStyle: { color: "#e5e7eb" } },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: "#6b7280", formatter: "{value}%" },
        splitLine: { lineStyle: { color: "#f1f5f9" } },
      },
      series,
    }, true);
  }

  // ---------- 区间交易参数估算 ----------
  bandForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      ts_code: bandInput.value.trim(),
      start_date: $("#band_start_date").value.trim() || "20170101",
      end_date: $("#band_end_date").value.trim() || "",
      initial_capital: parseFloat($("#band_capital").value) || 100000,
      min_sharpe: parseFloat($("#band_min_sharpe").value) || 1.0,
      objective: $("#band_objective").value,
      max_trades: parseInt($("#band_max_trades").value, 10) || 100,
    };
    if (!payload.ts_code) {
      bandError.textContent = "请输入股票代码。";
      bandError.classList.remove("hidden");
      return;
    }
    bandError.classList.add("hidden");
    bandResult.classList.add("hidden");
    bandBtn.disabled = true;
    bandLoading.classList.remove("hidden");
    $("#band-range-hint").textContent = "";
    try {
      const res = await fetch("/api/band/optimize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "估算失败");
      renderBand(data);
    } catch (err) {
      bandError.textContent = err.message || "请求失败, 请检查后端服务。";
      bandError.classList.remove("hidden");
    } finally {
      bandLoading.classList.add("hidden");
      bandBtn.disabled = false;
    }
  });

  function renderBand(data) {
    const { info, params, search, range, band, baseline } = data;
    bandResult.classList.remove("hidden");

    $("#band-result-title").innerHTML =
      `<a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(info.ts_code)}" title="查看详情">${info.name}</a> (${info.ts_code}) · 区间交易最优参数`;
    const achievedTxt = search.achieved ? "✅ 夏普达标" : "⚠️ 未达目标夏普 (已取折中)";
    const maxTradesTxt = search.max_trades ? ` · 最大交易 ≤ ${search.max_trades}` : "";
    $("#band-result-sub").textContent =
      `${fmtDate(range.start)} ~ ${fmtDate(range.end)} · ${range.bars} 个交易日 · ` +
      `目标 ${search.objective_label || "收益优先"} · ` +
      `搜索 ${search.tried} 组参数${maxTradesTxt} · 目标夏普 ≥ ${search.min_sharpe} · ${achievedTxt}`;
    $("#band-range-hint").textContent =
      `数据区间 ${fmtDate(range.start)} ~ ${fmtDate(range.end)} (${range.bars} 根)`;

    // 参数卡
    const m = band.metrics;
    const sellUp = ((params.sell_price / params.buy_price - 1) * 100).toFixed(0);
    const stopDown = ((1 - params.stop_price / params.buy_price) * 100).toFixed(0);
    $("#band-param-cards").innerHTML = `
      <div class="metric accent1">
        <div class="m-label">最优买入价</div>
        <div class="m-value">¥ ${params.buy_price.toFixed(2)}</div>
        <div class="m-sub">收盘价 ≤ 买入价 全仓买入</div>
      </div>
      <div class="metric accent2">
        <div class="m-label">最优卖出价</div>
        <div class="m-value">¥ ${params.sell_price.toFixed(2)}</div>
        <div class="m-sub">收盘价 ≥ 卖出价 清仓 (约 +${sellUp}%)</div>
      </div>
      <div class="metric accent3">
        <div class="m-label">最优止损价</div>
        <div class="m-value">¥ ${params.stop_price.toFixed(2)}</div>
        <div class="m-sub">收盘价 ≤ 止损价 清仓 (约 -${stopDown}%)</div>
      </div>
      <div class="metric ${cls(m.total_return)}">
        <div class="m-label">区间交易 · 总收益率</div>
        <div class="m-value">${fmtPct(m.total_return)}</div>
        <div class="m-sub">夏普 ${m.sharpe.toFixed(2)} · 回撤 ${m.max_drawdown.toFixed(2)}% · 卡玛 ${m.calmar.toFixed(2)}</div>
      </div>`;

    // 指标对比表
    const rows = [
      ["区间交易", "band", band],
      ["买入持有", "bh", baseline],
    ];
    $("#band-table tbody").innerHTML = rows.map(([name, tag, d]) => {
      const mm = d.metrics;
      return `<tr>
        <td><span class="tag ${tag}">${name}</span></td>
        <td class="num ${cls(mm.total_return)}">${fmtPct(mm.total_return)}</td>
        <td class="num ${cls(mm.annual_return)}">${fmtPct(mm.annual_return)}</td>
        <td class="num ${cls(-mm.max_drawdown)}">${mm.max_drawdown.toFixed(2)}%</td>
        <td class="num">${mm.calmar.toFixed(2)}</td>
        <td class="num">${mm.sharpe.toFixed(2)}</td>
        <td class="num">¥ ${fmtNum(mm.final_value)}</td>
      </tr>`;
    }).join("");

    renderBandTrades(data.trades || []);
    renderBandChart(band, baseline);
    bandResult.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------- 区间交易: 交易明细排序 / 筛选 ----------
  let bandTrades = [];
  const bandTradeSort = { by: "return_pct", order: "desc" };

  function renderBandTrades(trades) {
    bandTrades = trades || [];
    applyBandTradeFilter();
  }

  function applyBandTradeFilter() {
    const minRaw = $("#band-trade-min").value;
    const min = minRaw === "" ? null : parseFloat(minRaw);
    const type = $("#band-trade-type").value;

    // 筛选: 最低收益率 / 类型
    let list = bandTrades.filter((t) => {
      if (min !== null && !isNaN(min) && t.return_pct < min) return false;
      if (type && t.type !== type) return false;
      return true;
    });

    // 排序
    const { by, order } = bandTradeSort;
    const dir = order === "asc" ? 1 : -1;
    list = list.slice().sort((a, b) => {
      const va = a[by];
      const vb = b[by];
      if (typeof va === "string" && typeof vb === "string") {
        return va.localeCompare(vb) * dir;
      }
      return (va - vb) * dir;
    });

    const body = list.length
      ? list.map((t) => `
        <tr>
          <td>${t.no}</td>
          <td>${fmtDate(t.buy_date)}</td>
          <td class="num">${Number(t.buy_price).toFixed(4)}</td>
          <td>${fmtDate(t.sell_date)}</td>
          <td class="num">${Number(t.sell_price).toFixed(4)}</td>
          <td class="num ${cls(t.return_pct)}">${fmtPct(t.return_pct)}</td>
          <td><span class="tag ${t.type === "止损" ? "low" : "band"}">${t.type}</span></td>
        </tr>`).join("")
      : `<tr><td colspan="7" style="text-align:center;color:var(--text-soft)">无匹配交易</td></tr>`;
    $("#band-trades-table tbody").innerHTML = body;
    updateBandTradeSortArrows();
  }

  function updateBandTradeSortArrows() {
    document.querySelectorAll("#band-trades-table thead th.sortable").forEach((th) => {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      if (th.dataset.sort === bandTradeSort.by) {
        arrow.textContent = bandTradeSort.order === "desc" ? " ▼" : " ▲";
        th.classList.add("sorted");
      } else {
        arrow.textContent = "";
        th.classList.remove("sorted");
      }
    });
  }

  // 表头点击排序
  document.querySelectorAll("#band-trades-table thead th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const by = th.dataset.sort;
      if (bandTradeSort.by === by) {
        bandTradeSort.order = bandTradeSort.order === "desc" ? "asc" : "desc";
      } else {
        bandTradeSort.by = by;
        bandTradeSort.order = "desc";
      }
      applyBandTradeFilter();
    });
  });

  // 筛选控件
  $("#band-trade-min").addEventListener("input", () => applyBandTradeFilter());
  $("#band-trade-type").addEventListener("change", () => applyBandTradeFilter());

  function renderBandChart(band, baseline) {
    const el = $("#band-chart");
    if (!bandChart) bandChart = echarts.init(el);
    const dates = band.dates.map(fmtDate);
    const colors = ["#ea580c", "#4f46e5"];
    bandChart.setOption({
      animationDuration: 500,
      tooltip: {
        trigger: "axis",
        formatter: (params) => {
          let html = `<b>${params[0].axisValue}</b><br/>`;
          params.forEach((p) => {
            html += `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${p.color};margin-right:6px"></span>${p.seriesName}: <b>${p.value >= 0 ? "+" : ""}${p.value.toFixed(2)}%</b><br/>`;
          });
          return html;
        },
      },
      legend: { top: 4, textStyle: { fontSize: 13 } },
      grid: { left: 14, right: 20, top: 44, bottom: 36, containLabel: true },
      xAxis: {
        type: "category",
        data: dates,
        boundaryGap: false,
        axisLabel: { color: "#6b7280", fontSize: 11 },
        axisLine: { lineStyle: { color: "#e5e7eb" } },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: "#6b7280", formatter: "{value}%" },
        splitLine: { lineStyle: { color: "#f1f5f9" } },
      },
      series: [
        { name: "区间交易", type: "line", data: band.returns_pct, showSymbol: false, smooth: true, lineStyle: { width: 2.4, color: colors[0] }, itemStyle: { color: colors[0] }, areaStyle: { opacity: 0.07 } },
        { name: "买入持有", type: "line", data: baseline.returns_pct, showSymbol: false, smooth: true, lineStyle: { width: 2.2, color: colors[1] }, itemStyle: { color: colors[1] }, areaStyle: { opacity: 0.04 } },
      ],
    }, true);
    requestAnimationFrame(() => bandChart && bandChart.resize());
  }

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.classList.remove("hidden");
  }
  function hideError() {
    errorBox.classList.add("hidden");
    errorBox.textContent = "";
  }

  $("#reset-btn").addEventListener("click", () => {
    form.reset();
    $("#lookback_days").value = 20;
    $("#initial_capital").value = 100000;
    $("#start_date").value = "20170101";
    $("#gain_threshold").value = 20;
    tsTip.textContent = "";
    stockList.innerHTML = "";
    resultBox.classList.add("hidden");
    hideQuote();
    hideError();
  });

  // ---------- 财报分析 ----------
  caibaoForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const curY = new Date().getFullYear();
    const payload = {
      ts_code: caibaoInput.value.trim(),
      start_year: parseInt($("#caibao_start_year").value, 10) || (curY - 5),
      end_year: parseInt($("#caibao_end_year").value, 10) || (curY - 1),
      use_llm: $("#caibao_use_llm").value === "true",
    };
    if (!payload.ts_code) {
      caibaoError.textContent = "请输入股票代码。";
      caibaoError.classList.remove("hidden");
      return;
    }
    if (payload.end_year < payload.start_year) {
      caibaoError.textContent = "结束年份不能小于起始年份。";
      caibaoError.classList.remove("hidden");
      return;
    }
    caibaoError.classList.add("hidden");
    caibaoResult.classList.add("hidden");
    caibaoBtn.disabled = true;
    caibaoLoading.classList.remove("hidden");
    $("#caibao-hint").textContent = "";
    try {
      const res = await fetch("/api/caibao/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "分析失败");
      renderCaibao(data);
    } catch (err) {
      caibaoError.textContent = err.message || "请求失败, 请检查后端服务。";
      caibaoError.classList.remove("hidden");
    } finally {
      caibaoLoading.classList.add("hidden");
      caibaoBtn.disabled = false;
    }
  });

  function renderCaibao(data) {
    caibaoResult.classList.remove("hidden");
    $("#caibao-title").textContent = `${data.info.name} (${data.info.ts_code}) · 财报分析`;
    const isHk = String(data.info.ts_code).endsWith(".HK");
    $("#caibao-sub").textContent =
      `${data.range.start}~${data.range.end} 年 · ` +
      (data.llm_used ? "🤖 LLM 深度分析" : "📊 TUSHARE 财报数据分析") +
      (isHk ? " · 东财港股数据" : "");
    $("#caibao-meta").textContent = data.llm_used
      ? "基于财报分析框架由大模型生成, 仅供参考"
      : "基于 tushare 财务指标, 仅供参考";
    $("#caibao-report").innerHTML = marked.parse(data.markdown || "");
    renderCaibaoCards(data);
    enhanceCaibaoReport();
    buildCaibaoToc();
    $("#caibao-hint").textContent =
      `数据来源: ${isHk ? "东方财富港股" : "tushare"} · 已保存至本地 PostgreSQL (financial_data 表)`;
    caibaoResult.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // 报告输出优化: 章节重排(1→8) + 关键章节高亮 + 投资建议标签
  function enhanceCaibaoReport() {
    const body = $("#caibao-report");
    if (!body) return;
    const kids = [...body.children];
    const h2Idx = [];
    kids.forEach((el, i) => { if (el.tagName === "H2") h2Idx.push(i); });
    // 1) 章节按序号 1→8 重排 (每个 h2 及其后内容为一个章节块)
    if (h2Idx.length >= 2) {
      const sections = [];
      h2Idx.forEach((idx, s) => {
        const end = (h2Idx[s + 1] !== undefined) ? h2Idx[s + 1] : kids.length;
        const block = document.createElement("div");
        block.className = "cb-section";
        kids.slice(idx, end).forEach((el) => block.appendChild(el));
        const title = block.querySelector("h2");
        const num = parseInt((title.textContent.match(/(\d+)\./) || [])[1] || "99", 10);
        sections.push({ num, block });
      });
      body.innerHTML = "";
      sections.sort((a, b) => a.num - b.num).forEach((s) => body.appendChild(s.block));
    }
    // 2) 关键章节高亮 + 3) 投资建议结论标签
    body.querySelectorAll(".cb-section").forEach((block) => {
      const title = block.querySelector("h2").textContent || "";
      let cls = "";
      if (title.includes("执行摘要")) cls = "cb-summary";
      else if (title.includes("风险提示")) cls = "cb-risk";
      else if (title.includes("投资建议")) cls = "cb-advice";
      if (cls) block.classList.add(cls);
      if (cls === "cb-advice") {
        const order = ["强烈推荐", "买入", "增持", "推荐", "持有", "观望", "回避", "卖出", "谨慎"];
        const text = block.textContent || "";
        let hit = null;
        for (const w of order) { if (text.includes(w)) { hit = w; break; } }
        if (hit) {
          const badge = document.createElement("span");
          const good = ["强烈推荐", "买入", "增持", "推荐"].includes(hit);
          const bad = ["回避", "卖出", "谨慎"].includes(hit);
          badge.className = "cb-badge" + (good ? " good" : bad ? " bad" : "");
          badge.textContent = hit;
          block.querySelector("h2").appendChild(badge);
        }
      }
    });
  }

  // 核心指标概览卡片 (从 financials / valuation 渲染, 复用 .metric-cards 样式)
  function renderCaibaoCards(data) {
    const cards = $("#caibao-cards");
    const fin = data.financials || {};
    const val = data.valuation || {};
    const years = Object.keys(fin).sort();
    if (!years.length) { cards.innerHTML = ""; return; }
    const last = fin[years[years.length - 1]] || {};
    const first = fin[years[0]] || {};
    const num = (v) => (v === null || v === undefined || isNaN(Number(v))) ? null : Number(v);
    const fmt2 = (v, suffix = "") => (v === null || v === undefined || isNaN(Number(v))) ? "—" : Number(v).toFixed(2) + suffix;
    const growth = (k) => { const a = num(first[k]), b = num(last[k]); return (a === null || b === null || a === 0) ? null : ((b - a) / Math.abs(a)) * 100; };
    const delta = (k) => { const a = num(first[k]), b = num(last[k]); return (a === null || b === null) ? null : (b - a); };
    const trendSub = (k, reverse, suffix = "") => {
      const g = growth(k); if (g === null) return "";
      const good = reverse ? g < 0 : g > 0;
      return `<span class="m-sub ${good ? "t-up" : "t-down"}">区间 ${g >= 0 ? "+" : ""}${g.toFixed(1)}%</span>`;
    };
    const diffSub = (k, reverse, suffix = "pp") => {
      const d = delta(k); if (d === null) return "";
      const good = reverse ? d < 0 : d > 0;
      return `<span class="m-sub ${good ? "t-up" : "t-down"}">区间 ${d >= 0 ? "+" : ""}${d.toFixed(1)}${suffix}</span>`;
    };
    const items = [
      { label: "营业收入", value: fmt2(last["total_revenue_亿"], " 亿"), sub: trendSub("total_revenue_亿") },
      { label: "净利润", value: fmt2(last["net_income_亿"], " 亿"), sub: trendSub("net_income_亿") },
      { label: "毛利率", value: fmt2(last["gross_margin_%"], "%"), sub: diffSub("gross_margin_%") },
      { label: "净利率", value: fmt2(last["net_margin_%"], "%"), sub: diffSub("net_margin_%") },
      { label: "ROE", value: fmt2(last["roe_%"], "%"), sub: diffSub("roe_%") },
      { label: "资产负债率", value: fmt2(last["debt_to_assets_%"], "%"), sub: diffSub("debt_to_assets_%", true) },
      { label: "净现比", value: fmt2(last["净现比"]), sub: (num(last["净现比"]) !== null && num(last["净现比"]) >= 1) ? '<span class="m-sub t-up">≥1 现金流较健康</span>' : '<span class="m-sub t-down">＜1 关注利润含金量</span>' },
      { label: "经营现金流", value: fmt2(last["ocf_亿"], " 亿"), sub: trendSub("ocf_亿") },
    ];
    if (val && (num(val.pe_ttm) !== null || num(val.pb) !== null)) {
      items.push({
        label: "估值",
        value: "PE " + fmt2(val.pe_ttm) + " · PB " + fmt2(val.pb),
        sub: (num(val["dv_ratio_%"]) !== null) ? `股息率 ${fmt2(val["dv_ratio_%"], "%")}` : "",
      });
    }
    cards.innerHTML = items.map((it, i) =>
      `<div class="metric accent${(i % 3) + 1}"><div class="m-label">${it.label}</div><div class="m-value">${it.value}</div>${it.sub || ""}</div>`
    ).join("");
  }

  // 报告目录锚点 (为 h2/h3 生成锚点 + 目录导航)
  function buildCaibaoToc() {
    const body = $("#caibao-report");
    const toc = $("#caibao-toc");
    const heads = body.querySelectorAll("h2, h3");
    if (!heads.length) { toc.classList.add("hidden"); toc.innerHTML = ""; return; }
    const items = [];
    heads.forEach((h, i) => {
      const id = "cb-sec-" + i;
      h.id = id;
      items.push(`<a class="toc-${h.tagName.toLowerCase()}" href="#${id}">${h.textContent}</a>`);
    });
    toc.innerHTML = '<span class="toc-title">目录</span>' + items.join("");
    toc.classList.remove("hidden");
  }

  // ---------- 策略 Hub (精选策略, 参考问财经典策略) ----------
  let stratList = [];
  const STRAT_TABLE_COLS = {
    rlv: [["name", "名称"], ["ts_code", "代码"], ["dividend_yield_ttm", "股息率TTM%"], ["dividend_yield", "静态股息率%"], ["volatility", "波动率%"], ["div_per_share", "每股分红"], ["payout_ratio", "分红率%"], ["roe", "ROE%"], ["debt_to_assets", "资产负债率%"]],
    fund: [["name", "名称"], ["ts_code", "代码"], ["roe", "ROE%"], ["net_margin", "净利率%"], ["gross_margin", "毛利率%"], ["assets_turn", "总资产周转"], ["debt_to_assets", "资产负债率%"]],
    etf: [["name", "名称"], ["ts_code", "代码"], ["scale", "规模(亿)"], ["m_fee", "管理费%"], ["c_fee", "托管费%"], ["premium", "折溢价%"], ["avg_amount_20", "日均成交额(万)"], ["pos52", "52周位置"]],
    hk_rlv: [["name", "名称"], ["ts_code", "代码"], ["dividend_yield_ttm", "股息率TTM%"], ["dividend_yield", "静态股息率%"], ["volatility", "波动率%"], ["roe", "ROE%"], ["payout_ratio", "分红率%"]],
  };
  function _stratCardHtml(it) {
    const bt = (it.backtest && it.backtest.metrics) || null;
    const btHtml = bt
      ? `<div class="strat-bt"><span><em>年化</em><b>${bt.annual_pct != null ? fmtPct(bt.annual_pct, 1) : "—"}</b></span><span><em>累计</em><b>${bt.cum_pct != null ? fmtPct(bt.cum_pct, 1) : "—"}</b></span><span><em>最大回撤</em><b>${bt.max_dd_pct != null ? bt.max_dd_pct.toFixed(1) + "%" : "—"}</b></span></div>`
      : `<div class="strat-bt strat-bt-empty"><span>点击查看选股结果</span></div>`;
    return `<div class="strat-card" data-key="${it.key}">
      <div class="strat-name">${it.name}</div>
      <div class="strat-tags">${(it.tags || []).map((t) => `<span>${t}</span>`).join("")}</div>
      <p class="strat-desc">${it.desc}</p>${btHtml}</div>`;
  }
  let customStrategies = [];
  function renderStrategyCards(items) {
    stratList = items || [];
    const cats = [];
    stratList.forEach((it) => { if (!cats.includes(it.category)) cats.push(it.category); });
    let html = cats.map((cat) =>
      `<div class="strat-cat"><h3 class="strat-cat-title">${cat}</h3><div class="strat-grid">` +
      stratList.filter((it) => it.category === cat).map(_stratCardHtml).join("") +
      `</div></div>`).join("");
    // 我的策略 (自定义, 从选股保存或手动创建)
    html += `<div class="strat-cat"><h3 class="strat-cat-title">我的策略 <span class="hint">${customStrategies.length ? `(${customStrategies.length})` : "从「选股」tab 筛选后点「＋ 存入策略Hub」保存"}</span></h3><div class="strat-grid">`;
    if (customStrategies.length) {
      html += customStrategies.map(_customCardHtml).join("");
    } else {
      html += `<div class="strat-card strat-custom-empty">
        <div class="strat-name">新建自定义策略</div>
        <p class="strat-desc">在「选股」tab 筛选公司后点「＋ 存入策略Hub」即可保存为公司列表; 也可手动创建。</p>
        <div class="strat-ops"><button type="button" class="strat-op" id="strat-create-btn">＋ 手动创建</button></div>
      </div>`;
    }
    html += `</div></div>`;
    $("#strategy-cards").innerHTML = html;
    document.querySelectorAll("#strategy-cards .strat-card[data-key]").forEach((card) => {
      card.addEventListener("click", () => runStrategy(card.dataset.key, card));
    });
    // 自定义策略卡片: 点击查看公司 / 操作按钮
    document.querySelectorAll("#strategy-cards .strat-card[data-cid]").forEach((card) => {
      card.addEventListener("click", (e) => {
        if (e.target.closest(".strat-op")) return;
        viewCustomStrategy(Number(card.dataset.cid));
      });
      card.querySelectorAll(".strat-op").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const op = btn.dataset.op;
          const cid = Number(card.dataset.cid);
          if (op === "view") viewCustomStrategy(cid);
          else if (op === "edit") editCustomStrategy(cid);
          else if (op === "del") deleteCustomStrategy(cid);
        });
      });
    });
    const createBtn = $("#strat-create-btn");
    if (createBtn) createBtn.addEventListener("click", createCustomStrategy);
  }
  function _customCardHtml(it) {
    return `<div class="strat-card strat-custom" data-cid="${it.id}">
      <div class="strat-name">${it.name} <span class="strat-src">${it.source === "screener" ? "选股保存" : "手动"}</span></div>
      <div class="strat-tags"><span>${it.stock_count || 0} 家公司</span></div>
      <p class="strat-desc">${it.desc_text || "（无描述）"}</p>
      <div class="strat-ops">
        <button type="button" class="strat-op" data-op="view">查看公司</button>
        <button type="button" class="strat-op" data-op="edit">编辑</button>
        <button type="button" class="strat-op danger" data-op="del">删除</button>
      </div></div>`;
  }
  async function loadStrategies() {
    const items = [];
    try {
      const r = await fetch("/api/strategy/list");
      const d = await r.json();
      items.push(...(d.items || []));
    } catch (e) { /* 忽略 */ }
    try {
      const r2 = await fetch("/api/custom/strategy/list");
      const d2 = await r2.json();
      customStrategies = d2.items || [];
    } catch (e) { customStrategies = []; }
    renderStrategyCards(items);
  }
  async function createCustomStrategy() {
    const name = prompt("新建策略名称:", "");
    if (!name || !name.trim()) return;
    const desc = prompt("策略描述 (可选):", "") || "";
    try {
      const r = await fetch("/api/custom/strategy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim(), desc }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "创建失败");
      loadStrategies();
      viewCustomStrategy(d.id);
    } catch (e) { alert(e.message || "创建失败"); }
  }
  async function editCustomStrategy(cid) {
    const it = customStrategies.find((x) => x.id === cid);
    if (!it) return;
    const name = prompt("策略名称:", it.name);
    if (name === null) return;
    const desc = prompt("策略描述:", it.desc_text || "");
    if (desc === null) return;
    try {
      const r = await fetch("/api/custom/strategy/" + cid, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim(), desc }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "更新失败");
      loadStrategies();
    } catch (e) { alert(e.message || "更新失败"); }
  }
  async function deleteCustomStrategy(cid) {
    if (!window.confirm("确定删除该自定义策略及其全部公司？")) return;
    try {
      const r = await fetch("/api/custom/strategy/" + cid, { method: "DELETE" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "删除失败");
      $("#custom-strategy-result").classList.add("hidden");
      loadStrategies();
    } catch (e) { alert(e.message || "删除失败"); }
  }
  async function viewCustomStrategy(cid) {
    const it = customStrategies.find((x) => x.id === cid);
    const res = $("#custom-strategy-result");
    res.classList.remove("hidden");
    $("#custom-st-title").textContent = it ? it.name : "我的策略";
    $("#custom-st-sub").textContent = it ? `${it.desc_text || ""} · ${it.stock_count || 0} 家公司`.trim() : "";
    document.querySelector("#custom-st-table tbody").innerHTML = "";
    try {
      const r = await fetch("/api/custom/strategy/" + cid + "/stocks");
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "加载失败");
      const rows = (d.items || []).map((s) =>
        `<tr><td><a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(s.ts_code)}" target="_blank">${s.name || s.ts_code}</a></td><td>${s.ts_code}</td><td><button type="button" class="strat-op danger" data-rm="${s.ts_code}">移除</button></td></tr>`).join("") ||
        `<tr><td colspan="3" class="empty">该策略暂无公司</td></tr>`;
      document.querySelector("#custom-st-table tbody").innerHTML = rows;
      // 副标题用实际公司数 (移除/添加后立即刷新)
      $("#custom-st-sub").textContent = it ? `${it.desc_text || ""} · ${(d.items || []).length} 家公司`.trim() : "";
      document.querySelector("#custom-st-table tbody").querySelectorAll("[data-rm]").forEach((btn) => {
        btn.addEventListener("click", () => removeCustomStock(cid, btn.dataset.rm));
      });
      const addBtn = $("#custom-st-add-btn");
      const codeInp = $("#custom-st-code");
      const doAdd = async () => {
        const code = codeInp.value.trim();
        if (!code) return;
        try {
          const rr = await fetch("/api/custom/strategy/" + cid + "/stocks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ts_code: code }) });
          const dd = await rr.json();
          if (!rr.ok) throw new Error(dd.detail || "添加失败");
          codeInp.value = "";
          viewCustomStrategy(cid);
          loadStrategies();
        } catch (e) { alert(e.message || "添加失败"); }
      };
      addBtn.onclick = doAdd;
      codeInp.onkeydown = (e) => { if (e.key === "Enter") doAdd(); };
      // 该策略的公司列表 → Alpha158 回测
      const a158Btn = $("#custom-st-a158-btn");
      if (a158Btn) a158Btn.onclick = () => runAlpha158WithStrategy(d.items || [], (it && it.name) || "我的策略");
      // 该策略的公司列表 → 组合回测 (买入持有 + 区间交易)
      const ptfBtn2 = $("#custom-st-ptf-btn");
      if (ptfBtn2) ptfBtn2.onclick = () => runPortfolioWithStrategy(d.items || [], (it && it.name) || "我的策略");
      res.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      $("#custom-st-sub").textContent = e.message || "加载失败";
    }
  }
  async function removeCustomStock(cid, code) {
    try {
      const r = await fetch("/api/custom/strategy/" + cid + "/stocks/" + encodeURIComponent(code), { method: "DELETE" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "移除失败");
      viewCustomStrategy(cid);
      loadStrategies();
    } catch (e) { alert(e.message || "移除失败"); }
  }
  async function runStrategy(key, card) {
    document.querySelectorAll("#strategy-cards .strat-card").forEach((c) => c.classList.toggle("active", c === card));
    const res = $("#strategy-result");
    res.classList.remove("hidden");
    $("#strategy-title").textContent = "策略加载中…";
    $("#strategy-sub").textContent = "";
    $("#strategy-table tbody").innerHTML = "";
    try {
      const r = await fetch("/api/strategy/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, limit: 20 }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "策略执行失败");
      renderStrategyResult(d);
      // 触发回测参考指标 (后台计算, 完成后刷新卡片)
      fetch("/api/strategy/backtest/" + key).then((rr) => rr.json()).then((bt) => {
        const idx = stratList.findIndex((x) => x.key === key);
        if (idx >= 0) stratList[idx].backtest = bt;
        renderStrategyCards(stratList);
        const sel = document.querySelector(`#strategy-cards .strat-card[data-key="${key}"]`);
        if (sel) sel.classList.add("active");
      }).catch(() => { /* 回测失败不影响展示 */ });
    } catch (e) {
      $("#strategy-sub").textContent = e.message || "策略执行失败";
    }
  }
  function renderStrategyResult(d) {
    const s = d.strategy || {};
    $("#strategy-title").textContent = s.name || "策略";
    $("#strategy-sub").textContent = `${s.desc || ""} · 共 ${d.meta ? d.meta.count : 0} 条`;
    const cols = STRAT_TABLE_COLS[s.type] || STRAT_TABLE_COLS.rlv;
    document.querySelector("#strategy-table thead").innerHTML =
      "<tr>" + cols.map((c) => `<th>${c[1]}</th>`).join("") + "</tr>";
    const link = (it) => `<a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(it.ts_code)}" target="_blank">${it.name}</a>`;
    const rows = (d.items || []).map((it) => {
      const tds = cols.map((c) => {
        if (c[0] === "name") return `<td>${link(it)}</td>`;
        const v = it[c[0]];
        const txt = (v === null || v === undefined || v === "") ? "—"
          : (isNaN(Number(v)) ? v : (Math.abs(Number(v)) >= 100 ? Number(v).toFixed(0) : Number(v).toFixed(2)));
        return `<td>${txt}</td>`;
      });
      return `<tr>${tds.join("")}</tr>`;
    }).join("");
    document.querySelector("#strategy-table tbody").innerHTML = rows ||
      `<tr><td colspan="${cols.length}" class="empty">暂无符合条件的标的</td></tr>`;
    // 该策略筛选出的公司 → Alpha158 回测
    const a158Btn = $("#strategy-a158-btn");
    if (a158Btn) a158Btn.onclick = () => runAlpha158WithStrategy(d.items || [], (s && s.name) || "精选策略");
    // 该策略筛选出的公司 → 组合回测 (买入持有 + 区间交易)
    const ptfBtn2 = $("#strategy-ptf-btn");
    if (ptfBtn2) ptfBtn2.onclick = () => runPortfolioWithStrategy(d.items || [], (s && s.name) || "精选策略");
    $("#strategy-result").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  // ---------- 精选思想 (投资大师/方法 skill, 可增删改查) ----------
  let ideaList = [];
  async function loadIdeas() {
    try {
      const r = await fetch("/api/ideas/list");
      const d = await r.json();
      ideaList = d.items || [];
      renderIdeas();
    } catch (e) { /* 忽略 */ }
  }
  function renderIdeas() {
    const schools = [];
    ideaList.forEach((it) => { const s = it.school || "其他"; if (!schools.includes(s)) schools.push(s); });
    const html = schools.map((s) =>
      `<div class="strat-cat"><h3 class="strat-cat-title">${s}</h3><div class="strat-grid">` +
      ideaList.filter((it) => (it.school || "其他") === s).map(_ideaCardHtml).join("") +
      `</div></div>`).join("") || `<p class="hint" style="padding:8px">暂无思想, 点击「＋ 新建思想」创建</p>`;
    $("#idea-cards").innerHTML = html;
    document.querySelectorAll("#idea-cards .idea-card").forEach((card) => {
      card.querySelectorAll(".strat-op").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const op = btn.dataset.op;
          const id = Number(card.dataset.id);
          if (op === "toggle") card.querySelector(".idea-principles").classList.toggle("hidden");
          else if (op === "edit") editIdea(id);
          else if (op === "del") deleteIdea(id);
        });
      });
    });
  }
  function _ideaCardHtml(it) {
    const tags = (it.tags || []).map((t) => `<span>${t}</span>`).join("");
    const principles = (it.principles || "").split("\n").filter((s) => s.trim()).map((p) => `<li>${p}</li>`).join("");
    return `<div class="idea-card" data-id="${it.id}">
      <div class="idea-head"><span class="idea-name">${it.name}</span><span class="idea-school">${it.school || "其他"}</span></div>
      <div class="strat-tags">${tags}</div>
      <p class="idea-bio">${it.bio || ""}</p>
      <ul class="idea-principles hidden">${principles || "<li>暂无核心理念</li>"}</ul>
      <div class="strat-ops">
        <button type="button" class="strat-op" data-op="toggle">核心理念</button>
        <button type="button" class="strat-op" data-op="edit">编辑</button>
        <button type="button" class="strat-op danger" data-op="del">删除</button>
      </div></div>`;
  }
  // 思想编辑模态框 (一个页面多字段输入, 替代多次 prompt)
  let editingIdeaId = null;
  const ideaModal = $("#idea-modal");

  function openIdeaModal(idea) {
    editingIdeaId = idea ? idea.id : null;
    $("#idea-modal-title").textContent = idea ? "编辑思想" : "新建思想";
    $("#idea_f_name").value = idea ? (idea.name || "") : "";
    $("#idea_f_school").value = idea ? (idea.school || "") : "";
    $("#idea_f_tags").value = idea ? (idea.tags || []).join(", ") : "";
    $("#idea_f_bio").value = idea ? (idea.bio || "") : "";
    $("#idea_f_principles").value = idea ? (idea.principles || "") : "";
    // 把已使用过的流派并入下拉候选 (含自定义), 避免重复
    const dl = $("#idea-schools");
    if (dl) {
      const known = new Set(Array.from(dl.querySelectorAll("option")).map((o) => o.value));
      ideaList.forEach((it) => {
        const s = (it.school || "").trim();
        if (s && !known.has(s)) {
          const o = document.createElement("option");
          o.value = s;
          dl.appendChild(o);
          known.add(s);
        }
      });
    }
    ideaModal.classList.remove("hidden");
    $("#idea_f_name").focus();
  }
  function closeIdeaModal() {
    if (ideaModal) ideaModal.classList.add("hidden");
  }
  async function saveIdea() {
    const name = $("#idea_f_name").value.trim();
    if (!name) { alert("请填写人物/方法名"); $("#idea_f_name").focus(); return; }
    const payload = {
      name,
      school: $("#idea_f_school").value.trim(),
      tags: $("#idea_f_tags").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      bio: $("#idea_f_bio").value.trim(),
      principles: $("#idea_f_principles").value,
    };
    try {
      const url = editingIdeaId ? "/api/ideas/" + editingIdeaId : "/api/ideas";
      const method = editingIdeaId ? "PUT" : "POST";
      const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || (editingIdeaId ? "更新失败" : "创建失败"));
      closeIdeaModal();
      loadIdeas();
    } catch (e) { alert(e.message || "保存失败"); }
  }
  function createIdea() { openIdeaModal(null); }
  function editIdea(id) {
    const it = ideaList.find((x) => x.id === id);
    if (!it) return;
    openIdeaModal(it);
  }
  const ideaSaveBtn = $("#idea-save-btn");
  if (ideaSaveBtn) ideaSaveBtn.addEventListener("click", saveIdea);
  const ideaCancelBtn = $("#idea-cancel-btn");
  if (ideaCancelBtn) ideaCancelBtn.addEventListener("click", closeIdeaModal);
  if (ideaModal) {
    ideaModal.addEventListener("click", (e) => { if (e.target === ideaModal) closeIdeaModal(); });
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && ideaModal && !ideaModal.classList.contains("hidden")) closeIdeaModal();
  });
  async function deleteIdea(id) {
    if (!window.confirm("确定删除该思想？")) return;
    try {
      const r = await fetch("/api/ideas/" + id, { method: "DELETE" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "删除失败");
      loadIdeas();
    } catch (e) { alert(e.message || "删除失败"); }
  }
  const ideaCreateBtn = $("#idea-create-btn");
  if (ideaCreateBtn) ideaCreateBtn.addEventListener("click", createIdea);

  // 策略Hub: 精选策略 / 精选思想 分段切换 (并列展示, 点击切换)
  const hubResultState = { strategy: false, custom: false };
  function setHubPanel(panel) {
    if (panel !== "strategies") {
      hubResultState.strategy = !$("#strategy-result").classList.contains("hidden");
      hubResultState.custom = !$("#custom-strategy-result").classList.contains("hidden");
      $("#strategy-result").classList.add("hidden");
      $("#custom-strategy-result").classList.add("hidden");
    } else {
      if (hubResultState.strategy) $("#strategy-result").classList.remove("hidden");
      if (hubResultState.custom) $("#custom-strategy-result").classList.remove("hidden");
    }
    document.querySelectorAll("#seg-hub .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.panel === panel));
    document.querySelectorAll("#tab-strategies .hub-panel").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== panel));
  }
  document.querySelectorAll("#seg-hub .seg-btn").forEach((b) => b.addEventListener("click", () => setHubPanel(b.dataset.panel)));
  setHubPanel("strategies");

  loadStrategies();
  loadIdeas();

  // ---------- ETF 筛选 ----------
  const _efmt = (v, d = 2, suffix = "") =>
    (v === null || v === undefined || isNaN(Number(v))) ? "—" : Number(v).toFixed(d) + suffix;
  const _esign = (v, d = 2, suffix = "") =>
    (v === null || v === undefined || isNaN(Number(v))) ? "—"
      : (v > 0 ? "+" : "") + Number(v).toFixed(d) + suffix;

  function _sortedEtf() {
    const k = etfSort.key;
    const dir = etfSort.order === "asc" ? 1 : -1;
    const arr = etfItems.slice();
    arr.sort((a, b) => {
      const va = a[k], vb = b[k];
      const aNull = va === null || va === undefined || va === "";
      const bNull = vb === null || vb === undefined || vb === "";
      if (aNull && bNull) return 0;
      if (aNull) return 1;      // 空值排最后
      if (bNull) return -1;
      const na = parseFloat(va), nb = parseFloat(vb);
      let cmp;
      if (!isNaN(na) && !isNaN(nb)) cmp = na - nb;
      else cmp = String(va).localeCompare(String(vb), "zh");
      return cmp * dir;
    });
    return arr;
  }

  function updateEtfSortArrows() {
    document.querySelectorAll("#etf-table thead th.sortable").forEach((th) => {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      if (th.dataset.sort === etfSort.key) {
        arrow.textContent = etfSort.order === "desc" ? " ▼" : " ▲";
        th.classList.add("sorted");
      } else {
        arrow.textContent = "";
        th.classList.remove("sorted");
      }
    });
  }

  function _renderEtfTable() {
    const body = _sortedEtf().map((it) => `
      <tr>
        <td>${it.ts_code}</td>
        <td><a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(it.ts_code)}" title="查看详情">${it.name}</a></td>
        <td>${it.fund_type || "—"}</td>
        <td class="num">${_efmt(it.close, 3)}</td>
        <td class="num ${cls(it.pct_chg || 0)}">${_esign(it.pct_chg, 2, "%")}</td>
        <td class="num">${_efmt(it.scale, 2)}</td>
        <td class="num">${_efmt(it.m_fee, 2)}</td>
        <td class="num">${_efmt(it.c_fee, 2)}</td>
        <td class="num">${_efmt(it.avg_amount_20, 0)}</td>
        <td class="num ${cls(it.premium || 0)}">${_esign(it.premium, 2, "%")}</td>
        <td class="num">${_efmt(it.track_dev, 3, "%")}</td>
        <td class="num">${_efmt(it.pos52, 2)}</td>
        <td class="num">${_efmt(it.high52, 3)}</td>
        <td class="num">${_efmt(it.low52, 3)}</td>
        <td class="num">${_efmt(it.age_years, 1)}</td>
        <td class="num">${it.list_date || "—"}</td>
      </tr>`).join("")
      || `<tr><td colspan="16" style="text-align:center;color:var(--text-soft)">暂无符合条件的 ETF, 请放宽筛选条件</td></tr>`;
    $("#etf-table tbody").innerHTML = body;
    updateEtfSortArrows();
  }

  function renderEtf(data) {
    etfResult.classList.remove("hidden");
    const src = data.source === "db"
      ? ` · 数据来自初始化 (计算日 ${data.calc_date || "—"})`
      : (data.cached ? " · 命中缓存" : " · 已刷新数据");
    etfSub.textContent =
      `共 ${data.count} 只 · 扫描 ${(data.ok || 0) + (data.fail || 0)} 只 / 成功 ${data.ok || 0} 只` +
      src + " · 点击表头排序";
    etfItems = data.items || [];
    _renderEtfTable();
    etfResult.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function buildEtfPayload(refresh) {
    const num = (id) => {
      const v = parseFloat($(id).value);
      return isNaN(v) ? null : v;
    };
    return {
      keyword: $("#etf_keyword").value.trim(),
      fund_type: $("#etf_type").value,
      min_scale: num("#etf_min_scale"),
      max_m_fee: num("#etf_max_mfee"),
      max_c_fee: num("#etf_max_cfee"),
      min_amount_20: num("#etf_min_amount"),
      max_premium: num("#etf_max_premium"),
      limit: parseInt($("#etf_limit").value, 10) || 300,
      sort_by: etfSort.key,
      order: etfSort.order,
      refresh: !!refresh,
    };
  }

  async function runEtfScreen(refresh) {
    etfError.classList.add("hidden");
    etfResult.classList.add("hidden");
    etfBtn.disabled = true;
    etfRefreshBtn.disabled = true;
    etfLoading.classList.remove("hidden");
    $("#etf-hint").textContent = "";
    try {
      const res = await fetch("/api/etf/screen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildEtfPayload(refresh)),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "筛选失败");
      const srcTag = data.source === "db" ? " · 读取初始化数据"
        : (data.cached ? " · 命中缓存" : " · 已刷新");
      $("#etf-hint").textContent =
        `✓ 筛选完成: 匹配 ${data.count} 只 (成功 ${data.ok} / 失败 ${data.fail}${srcTag})`;
      renderEtf(data);
    } catch (err) {
      etfError.textContent = err.message || "请求失败, 请检查后端服务。";
      etfError.classList.remove("hidden");
    } finally {
      etfLoading.classList.add("hidden");
      etfBtn.disabled = false;
      etfRefreshBtn.disabled = false;
    }
  }

  etfForm.addEventListener("submit", (e) => {
    e.preventDefault();
    runEtfScreen(false);
  });
  etfRefreshBtn.addEventListener("click", () => runEtfScreen(true));

  // 表头点击排序
  document.querySelectorAll("#etf-table thead th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const by = th.dataset.sort;
      if (etfSort.key === by) {
        etfSort.order = etfSort.order === "desc" ? "asc" : "desc";
      } else {
        etfSort.key = by;
        etfSort.order = "desc";
      }
      _renderEtfTable();
    });
  });

  // ---------- 我的股票 (自选股) ----------
  let myItems = [];
  let mySort = { key: "last_close", order: "desc" };

  async function loadMyStocks() {
    // 首页仅登录后可访问: 未登录跳转独立登录页
    if (!window.CaiBaoAuth || !CaiBaoAuth.isLoggedIn()) {
      location.replace("/static/login.html");
      return;
    }
    myError.classList.add("hidden");
    myResult.classList.remove("hidden");
    myLoading.classList.remove("hidden");
    myRefreshBtn.disabled = true;
    try {
      const res = await fetch("/api/my_stocks");
      if (res.status === 401) { location.replace("/static/login.html"); return; }
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "获取失败");
      renderMyStocks(data.items || []);
      mySub.textContent = data.count ? `共 ${data.count} 只自选股 · 行情截至 ${data.items[0] && data.items[0].last_date || "—"} · 点击表头排序` : "";
    } catch (err) {
      myError.textContent = err.message || "请求失败, 请检查后端服务。";
      myError.classList.remove("hidden");
    } finally {
      myLoading.classList.add("hidden");
      myRefreshBtn.disabled = false;
    }
  }

  function _sortedMyStocks() {
    const arr = myItems.slice();
    const k = mySort.key;
    const dir = mySort.order === "asc" ? 1 : -1;
    arr.sort((a, b) => {
      let va = a[k], vb = b[k];
      let cmp;
      if (typeof va === "number" && typeof vb === "number") {
        cmp = va - vb;
      } else {
        const na = parseFloat(va), nb = parseFloat(vb);
        cmp = (!isNaN(na) && !isNaN(nb)) ? na - nb
          : String(va == null ? "" : va).localeCompare(String(vb == null ? "" : vb), "zh");
      }
      return cmp * dir;
    });
    return arr;
  }

  function _renderMyTable() {
    const body = _sortedMyStocks().map((it) => `
      <tr>
        <td><a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(it.ts_code)}" title="查看详情">${it.name}</a></td>
        <td>${it.ts_code}</td>
        <td>${it.industry || "—"}</td>
        <td class="num">${it.last_close == null ? "—" : Number(it.last_close).toFixed(2)}</td>
        <td class="num">${it.week52_low == null ? "—" : Number(it.week52_low).toFixed(2)}</td>
        <td class="num">${it.week52_high == null ? "—" : Number(it.week52_high).toFixed(2)}</td>
        <td class="num ${cls(it.pct_chg || 0)}">${it.pct_chg == null ? "—" : fmtPct(it.pct_chg)}</td>
        <td class="num">${it.pe_ttm == null ? "—" : Number(it.pe_ttm).toFixed(2)}</td>
        <td class="num">${fmtYi(it.total_mv)}</td>
        <td class="num ${cls(it.dividend_yield || 0)}">${it.dividend_yield == null ? "—" : Number(it.dividend_yield).toFixed(2) + "%"}</td>
        <td class="num">${it.div_per_share == null ? "—" : Number(it.div_per_share).toFixed(2)}</td>
        <td><button type="button" class="btn-ghost" data-remove="${it.ts_code}" style="padding:4px 10px;font-size:12px">删除</button></td>
      </tr>`).join("")
      || `<tr><td colspan="12" style="text-align:center;color:#9ca3af;padding:24px">暂无自选股, 请到股票详情页点击「加入我的股票」</td></tr>`;
    $("#my-table tbody").innerHTML = body;
    updateMySortArrows();
    // 删除按钮
    document.querySelectorAll("#my-table tbody button[data-remove]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await fetch("/api/my_stocks/remove", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ts_code: btn.dataset.remove }),
          });
        } catch (e) { /* 忽略 */ }
        loadMyStocks();
      });
    });
  }

  function renderMyStocks(items) {
    myItems = items || [];
    _renderMyTable();
  }

  function updateMySortArrows() {
    document.querySelectorAll("#my-table thead th.sortable").forEach((th) => {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      if (th.dataset.sort === mySort.key) {
        arrow.textContent = mySort.order === "desc" ? " ▼" : " ▲";
        th.classList.add("sorted");
      } else {
        arrow.textContent = "";
        th.classList.remove("sorted");
      }
    });
  }

  // 表头点击排序 (我的股票)
  document.querySelectorAll("#my-table thead th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const by = th.dataset.sort;
      if (mySort.key === by) {
        mySort.order = mySort.order === "desc" ? "asc" : "desc";
      } else {
        mySort.key = by;
        mySort.order = "desc";
      }
      _renderMyTable();
    });
  });

  myRefreshBtn.addEventListener("click", loadMyStocks);

  // ---------- 我的股票: 按名称/代码搜索并加入自选股 ----------
  const mySearchInput = document.getElementById("my-search-input");
  const mySearchBtn = document.getElementById("my-search-btn");
  const mySearchPanel = document.getElementById("my-search-panel");
  const _esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  function _kindLabel(kind) {
    if (kind === "fund") return "ETF/基金";
    if (kind === "hk") return "港股";
    return "股票";
  }

  let mySearchTimer = null;
  async function doMyStockSearch() {
    if (!mySearchInput || !mySearchPanel) return;
    if (!window.CaiBaoAuth || !CaiBaoAuth.isLoggedIn()) return;  // 联想时未登录静默
    const kw = mySearchInput.value.trim();
    if (!kw) { mySearchPanel.classList.add("hidden"); return; }
    mySearchPanel.classList.remove("hidden");
    mySearchPanel.innerHTML = '<div class="search-empty">搜索中…</div>';
    try {
      const r = await fetch(`/api/stock/search?keyword=${encodeURIComponent(kw)}&limit=20`);
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || "搜索失败");
      const items = data.items || [];
      if (!items.length) {
        mySearchPanel.innerHTML = '<div class="search-empty">未找到匹配的股票/ETF</div>';
        return;
      }
      mySearchPanel.innerHTML = items.map((it) => `
        <div class="search-item" data-code="${_esc(it.ts_code)}">
          <span class="si-name">${_esc(it.name)}</span>
          <span class="si-code">${_esc(it.ts_code)}</span>
          <span class="si-kind">${_kindLabel(it.kind)}</span>
          <button type="button" class="btn-ghost btn-sm si-add" data-code="${_esc(it.ts_code)}">＋ 加入</button>
        </div>`).join("");
      mySearchPanel.querySelectorAll(".si-add").forEach((btn) => {
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          await addMyStockFromSearch(btn.dataset.code, btn);
        });
      });
    } catch (err) {
      mySearchPanel.innerHTML = `<div class="search-empty">搜索失败: ${_esc(err.message || err)}</div>`;
    }
  }

  async function addMyStockFromSearch(ts_code, btn) {
    try {
      const r = await fetch("/api/my_stocks/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ts_code }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "添加失败");
      if (btn) {
        btn.textContent = d.ok === false ? "已在列表" : "✓ 已加入";
        btn.classList.add("added");
        btn.disabled = true;
      }
      loadMyStocks();
    } catch (err) {
      alert(err.message || "添加失败");
    }
  }

  if (mySearchBtn) mySearchBtn.addEventListener("click", () => {
    if (window.CaiBaoAuth && !CaiBaoAuth.isLoggedIn()) { if (CaiBaoAuth.requireLogin) CaiBaoAuth.requireLogin(); return; }
    doMyStockSearch();
  });
  if (mySearchInput) {
    mySearchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doMyStockSearch(); });
    // 输入即联想 (300ms debounce, 实时推荐候选)
    mySearchInput.addEventListener("input", () => {
      clearTimeout(mySearchTimer);
      mySearchTimer = setTimeout(doMyStockSearch, 300);
    });
    // 点击搜索组件外部时收起结果面板
    document.addEventListener("click", (e) => {
      const wrap = document.getElementById("my-search-wrap");
      if (wrap && !wrap.contains(e.target)) mySearchPanel.classList.add("hidden");
    });
  }

  // 我的股票 tab: 右上角「回测」下拉 (选择不同回测方法, 对当前自选股执行)
  (function initMyBacktestMenu() {
    const myBtBtn = document.getElementById("my-backtest-btn");
    const myBtMenu = document.getElementById("my-backtest-menu");
    if (!myBtBtn || !myBtMenu) return;
    myBtBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      myBtMenu.classList.toggle("hidden");
    });
    document.addEventListener("click", (e) => {
      if (!myBtBtn.contains(e.target) && !myBtMenu.contains(e.target)) {
        myBtMenu.classList.add("hidden");
      }
    });
    myBtMenu.querySelectorAll("button[data-method]").forEach((btn) => {
      btn.addEventListener("click", () => {
        myBtMenu.classList.add("hidden");
        const method = btn.dataset.method;
        const stocks = (myItems || []).map((it) => ({ ts_code: it.ts_code, name: it.name }));
        if (!stocks.length) { alert("暂无自选股, 请先添加"); return; }
        if (method === "alpha158") runAlpha158WithStrategy(stocks, "我的股票");
        else if (method === "ptf") runPortfolioWithStrategy(stocks, "我的股票");
      });
    });
  })();

  // ---------- 移动端下拉刷新 (我的股票) ----------
  (function initMyPullRefresh() {
    // 仅支持触摸的移动端启用
    const isTouch = ("ontouchstart" in window) || (navigator.maxTouchPoints || 0) > 0;
    if (!isTouch) return;
    const THRESHOLD = 64;
    const myTab = document.getElementById("tab-my");
    let startY = 0, pulling = false, pullDist = 0;
    let indicator = null;

    function ensureIndicator() {
      if (indicator) return indicator;
      indicator = document.createElement("div");
      indicator.className = "pull-indicator";
      indicator.innerHTML = '<span class="pi-arrow">↓</span><span class="pi-text">下拉刷新</span>';
      document.body.appendChild(indicator);
      return indicator;
    }
    function showIndicator(state) {
      const el = ensureIndicator();
      const arrow = el.querySelector(".pi-arrow");
      const text = el.querySelector(".pi-text");
      if (state === "pull") {
        arrow.textContent = "↓"; text.textContent = "下拉刷新";
        el.style.opacity = String(0.4 + 0.6 * Math.min(pullDist / THRESHOLD, 1));
      } else if (state === "ready") {
        arrow.textContent = "↑"; text.textContent = "释放刷新";
        el.style.opacity = "1";
      }
    }
    function hideIndicator() { if (indicator) indicator.style.opacity = "0"; }
    function myTabActive() { return !myTab.classList.contains("hidden"); }
    function setPullDist(v) {
      pullDist = v;
      myTab.style.transform = v > 0 ? `translateY(${Math.round(Math.min(v * 0.3, 24))}px)` : "";
    }

    document.addEventListener("touchstart", (e) => {
      if (!myTabActive() || window.scrollY > 0) { pulling = false; return; }
      startY = e.touches[0].clientY; pulling = true; pullDist = 0;
    }, { passive: true });

    document.addEventListener("touchmove", (e) => {
      if (!pulling) return;
      const dy = e.touches[0].clientY - startY;
      if (dy <= 0 || window.scrollY > 0) { setPullDist(0); hideIndicator(); pulling = false; return; }
      const d = Math.min(dy * 0.45, 100);
      setPullDist(d);
      showIndicator(d >= THRESHOLD ? "ready" : "pull");
    }, { passive: true });

    function endPull() {
      if (!pulling) return;
      pulling = false;
      const ok = pullDist >= THRESHOLD;
      setPullDist(0); hideIndicator(); pullDist = 0;
      if (ok) loadMyStocks();
    }
    document.addEventListener("touchend", endPull, { passive: true });
    document.addEventListener("touchcancel", endPull, { passive: true });
  })();

  // ---------- Alpha158 回测 ----------
  let a158PoolItems = [];
  let a158StockSort = { key: "total_return", order: "desc" };
  // 从「策略 Hub」导入的股票池 (非空时优先于我的自选股, 可在池内复位)
  let a158PoolOverride = null;

  async function loadAlpha158Pool() {
    if (!window.CaiBaoAuth || !CaiBaoAuth.isLoggedIn()) return;
    if (a158PoolOverride) {
      a158PoolItems = a158PoolOverride.items;
      renderAlpha158Pool();
      return;
    }
    try {
      const res = await fetch("/api/my_stocks");
      if (!res.ok) throw new Error("获取我的股票失败");
      const data = await res.json();
      a158PoolItems = data.items || [];
      renderAlpha158Pool();
    } catch (err) {
      a158PoolBox.innerHTML = `<span class="alpha-pool-loading">${err.message || "加载失败"}</span>`;
    }
  }

  // 策略 Hub 筛选结果 → Alpha158 股票池 (仅保留 A 股, 港股/ETF 不支持)
  function runAlpha158WithStrategy(stocks, title) {
    const aShares = (stocks || []).filter((s) => /\.(SH|SZ)$/i.test(s.ts_code || ""));
    if (!aShares.length) {
      alert("该策略结果中没有 A 股 (港股/ETF 暂不支持 Alpha158 回测)");
      return;
    }
    a158PoolOverride = { items: aShares, title: title || "策略" };
    a158PoolItems = aShares;
    renderAlpha158Pool();
    const tab = document.querySelector('.tab[data-tab="backtest"]');
    if (tab) tab.click();   // 切到回测 tab
    const seg = document.querySelector('#seg-backtest .seg-btn[data-panel="alpha158"]');
    if (seg) seg.click();   // 切到 Alpha158 子面板 (触发 loadAlpha158Pool, override 保留策略池)
    const hint = $("#a158-hint");
    if (hint) hint.textContent = `已从「${a158PoolOverride.title}」导入 ${aShares.length} 只 A 股 · 可调整参数后点击「运行 Alpha158 回测」`;
    const sec = document.getElementById("bt-alpha158");
    if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderAlpha158Pool() {
    const poolLabel = $("#a158-pool-label");
    if (a158PoolOverride) {
      // 策略导入的股票池: 只含 A 股
      if (poolLabel) poolLabel.textContent = `股票池 (${a158PoolOverride.title || "策略"})`;
      const stocks = a158PoolOverride.items;
      let html = `<div class="alpha-pool-source">已导入策略公司 ${stocks.length} 只 · ` +
        `<button type="button" class="strat-op" id="a158-pool-reset">用回我的自选股</button></div>`;
      html += stocks.map((it) => `
        <label class="alpha-pool-item" title="${it.ts_code}">
          <input type="checkbox" value="${it.ts_code}" checked />
          <span>${it.name || it.ts_code}</span><em>${it.ts_code}</em>
        </label>`).join("");
      a158PoolBox.innerHTML = html;
      const reset = $("#a158-pool-reset");
      if (reset) reset.onclick = () => { a158PoolOverride = null; loadAlpha158Pool(); };
      return;
    }
    if (poolLabel) poolLabel.textContent = "股票池 (我的股票 A 股)";
    const stocks = a158PoolItems.filter((it) => it.kind === "stock");
    const others = a158PoolItems.filter((it) => it.kind !== "stock");
    if (!a158PoolItems.length) {
      a158PoolBox.innerHTML = `<span class="alpha-pool-loading">暂无自选股, 请先到股票详情页添加</span>`;
      return;
    }
    let html = "";
    if (stocks.length) {
      html += stocks.map((it) => `
        <label class="alpha-pool-item" title="${it.ts_code}">
          <input type="checkbox" value="${it.ts_code}" checked />
          <span>${it.name}</span><em>${it.ts_code}</em>
        </label>`).join("");
    }
    if (others.length) {
      html += `<div class="alpha-pool-unsupported">不支持: ${others.map((it) => `${it.name}(${it.kind})`).join("、")}</div>`;
    }
    a158PoolBox.innerHTML = html;
  }

  function _selectedAlphaSymbols() {
    return Array.from(a158PoolBox.querySelectorAll("input[type=checkbox]:checked"))
      .map((c) => c.value);
  }

  a158Form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const symbols = _selectedAlphaSymbols();
    if (!symbols.length) {
      a158Error.textContent = "请至少选择一只股票。";
      a158Error.classList.remove("hidden");
      return;
    }
    const payload = {
      symbols,
      data_start: $("#a158_data_start").value.trim() || "20160101",
      test_start: $("#a158_test_start").value.trim() || "20190801",
      test_end: $("#a158_test_end").value.trim() || "",
      enter_threshold: parseFloat($("#a158_enter_thr").value) || 0,
      exit_threshold: $("#a158_exit_thr").value === "" ? null : (parseFloat($("#a158_exit_thr").value) || 0),
      min_holding: parseInt($("#a158_min_holding").value, 10) || 20,
      initial_capital: parseFloat($("#a158_capital").value) || 100000,
    };
    a158Error.classList.add("hidden");
    a158Result.classList.add("hidden");
    a158Btn.disabled = true;
    a158Loading.classList.remove("hidden");
    $("#a158-hint").textContent = `${symbols.length} 只股票 · ${payload.test_start} ~ ${payload.test_end || "最新"} · 买>${payload.enter_threshold} 卖<${payload.exit_threshold ?? payload.enter_threshold} 持${payload.min_holding}天`;
    try {
      const res = await fetch("/api/alpha158/backtest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Alpha158 回测失败");
      if (data.ok === false) throw new Error(data.error || "Alpha158 回测失败 (无可用股票)");
      renderAlpha158(data);
    } catch (err) {
      a158Error.textContent = err.message || "请求失败, 请检查后端服务。";
      a158Error.classList.remove("hidden");
    } finally {
      a158Loading.classList.add("hidden");
      a158Btn.disabled = false;
    }
  });

  function renderAlpha158(data) {
    const { portfolio, stocks = [], params = {}, skipped = [] } = data;
    const okStocks = stocks.filter((s) => s.ok);
    if (!okStocks.length) {
      // 全部失败: 明确提示而非静默全 0
      a158Result.classList.remove("hidden");
      const skippedTxt = skipped.length ? `跳过: ${skipped.map((s) => `${s.ts_code}${s.name ? "(" + s.name + ")" : ""}(${s.reason || "无数据"})`).join("、")}` : "";
      $("#a158-result-sub").textContent = `${params.test_start || "—"} ~ ${params.test_end || "最新"} · 无可用回测结果`;
      $("#a158-metrics").innerHTML = `
        <div class="metric">
          <div class="m-label">回测结果</div>
          <div class="m-value" style="font-size:16px;color:#9ca3af">无股票可回测</div>
          <div class="m-sub">${skippedTxt || "股票池数据不足或训练样本过少, 请调整参数后重试"}</div>
        </div>`;
      $("#a158-table tbody").innerHTML = `<tr><td colspan="11" style="text-align:center;color:#9ca3af;padding:24px">无回测结果</td></tr>`;
      a158Result.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    a158Result.classList.remove("hidden");

    const skippedTxt = skipped.length ? ` · 跳过 ${skipped.map((s) => s.ts_code).join("、")}` : "";
    $("#a158-result-sub").textContent =
      `${params.test_start || "—"} ~ ${params.test_end || "最新"} · 股票池 ${okStocks.length} 只 · ` +
      `买>${params.enter_threshold} 卖<${params.exit_threshold ?? params.enter_threshold} 持${params.min_holding}天` +
      skippedTxt;

    // 组合指标卡
    const sm = (portfolio && portfolio.strategy_metrics) || {};
    const bm = (portfolio && portfolio.buyhold_metrics) || {};
    $("#a158-metrics").innerHTML = `
      <div class="metric ${cls(sm.total_return || 0)}">
        <div class="m-label">策略 · 总收益率</div>
        <div class="m-value">${fmtPct(sm.total_return || 0)}</div>
        <div class="m-sub">年化 ${fmtPct(sm.annual_return || 0)} · 期末 ¥${fmtNum(sm.final_value || 0)}</div>
      </div>
      <div class="metric ${cls(sm.max_drawdown || 0)}">
        <div class="m-label">策略 · 最大回撤</div>
        <div class="m-value">${(sm.max_drawdown || 0).toFixed(2)}%</div>
        <div class="m-sub">夏普 ${(sm.sharpe || 0).toFixed(2)} · 卡玛 ${(sm.calmar || 0).toFixed(2)}</div>
      </div>
      <div class="metric ${cls(bm.total_return || 0)}">
        <div class="m-label">买入持有 · 总收益率</div>
        <div class="m-value">${fmtPct(bm.total_return || 0)}</div>
        <div class="m-sub">年化 ${fmtPct(bm.annual_return || 0)} · 夏普 ${(bm.sharpe || 0).toFixed(2)}</div>
      </div>
      <div class="metric ${cls((sm.total_return || 0) - (bm.total_return || 0))}">
        <div class="m-label">超额收益 (策略-买入持有)</div>
        <div class="m-value">${fmtPct((sm.total_return || 0) - (bm.total_return || 0))}</div>
        <div class="m-sub">回撤 ${(sm.max_drawdown || 0).toFixed(2)}% vs ${(bm.max_drawdown || 0).toFixed(2)}%</div>
      </div>`;

    // 组合曲线 + 回撤
    renderAlpha158Chart(portfolio);
    renderAlpha158Dd(portfolio);

    // 个股表
    a158StockItems = okStocks;
    _renderAlpha158Table();
    a158Result.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  let a158StockItems = [];

  function _alphaVal(it, key) {
    if (key === "name" || key === "ts_code") return it[key];
    const m = it.metrics || {};
    return m[key];
  }

  function _renderAlpha158Table() {
    const arr = a158StockItems.slice();
    const k = a158StockSort.key;
    const dir = a158StockSort.order === "asc" ? 1 : -1;
    arr.sort((a, b) => {
      const va = _alphaVal(a, k), vb = _alphaVal(b, k);
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
      return String(va == null ? "" : va).localeCompare(String(vb == null ? "" : vb), "zh") * dir;
    });
    const body = arr.map((it) => {
      const m = it.metrics || {};
      return `<tr>
        <td><a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(it.ts_code)}" title="查看详情">${it.name || it.ts_code}</a></td>
        <td>${it.ts_code}</td>
        <td class="num ${cls(m.total_return || 0)}">${fmtPct(m.total_return || 0)}</td>
        <td class="num ${cls(m.annual_return || 0)}">${fmtPct(m.annual_return || 0)}</td>
        <td class="num ${cls(-(m.max_drawdown || 0))}">${(m.max_drawdown || 0).toFixed(2)}%</td>
        <td class="num">${(m.sharpe || 0).toFixed(2)}</td>
        <td class="num">${(m.calmar || 0).toFixed(2)}</td>
        <td class="num">${m.n_trades || 0}</td>
        <td class="num">${(m.total_cost || 0).toFixed(2)}%</td>
        <td class="num">${((m.exposure || 0) * 100).toFixed(0)}%</td>
        <td>${it.start || ""}~${it.end || ""}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="11" style="text-align:center;color:#9ca3af;padding:24px">无可回测的股票</td></tr>`;
    $("#a158-table tbody").innerHTML = body;
    updateAlpha158SortArrows();
  }

  function updateAlpha158SortArrows() {
    document.querySelectorAll("#a158-table thead th.sortable").forEach((th) => {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      if (th.dataset.sort === a158StockSort.key) {
        arrow.textContent = a158StockSort.order === "desc" ? " ▼" : " ▲";
        th.classList.add("sorted");
      } else {
        arrow.textContent = "";
        th.classList.remove("sorted");
      }
    });
  }

  document.querySelectorAll("#a158-table thead th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const by = th.dataset.sort;
      if (a158StockSort.key === by) {
        a158StockSort.order = a158StockSort.order === "desc" ? "asc" : "desc";
      } else {
        a158StockSort.key = by;
        a158StockSort.order = "desc";
      }
      _renderAlpha158Table();
    });
  });

  function renderAlpha158Chart(pf) {
    const el = $("#a158-chart");
    if (!pf || !pf.dates || !pf.dates.length) return;
    if (!alphaChart) alphaChart = echarts.init(el);
    const dates = pf.dates.map(fmtDate);
    alphaChart.setOption({
      animationDuration: 400,
      tooltip: { trigger: "axis", valueFormatter: (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%" },
      legend: { top: 4, textStyle: { fontSize: 13 } },
      grid: { left: 14, right: 20, top: 40, bottom: 36, containLabel: true },
      xAxis: { type: "category", data: dates, boundaryGap: false, axisLabel: { color: "#6b7280", fontSize: 11 }, axisLine: { lineStyle: { color: "#e5e7eb" } } },
      yAxis: { type: "value", axisLabel: { color: "#6b7280", formatter: "{value}%" }, splitLine: { lineStyle: { color: "#f1f5f9" } } },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 8 }],
      series: [
        { name: "Alpha158 策略", type: "line", data: pf.strategy, showSymbol: false, smooth: true, lineStyle: { width: 2.4, color: "#e65050" }, itemStyle: { color: "#e65050" }, areaStyle: { opacity: 0.07 } },
        { name: "买入持有", type: "line", data: pf.buyhold, showSymbol: false, smooth: true, lineStyle: { width: 2.2, color: "#4f46e5" }, itemStyle: { color: "#4f46e5" }, areaStyle: { opacity: 0.04 } },
      ],
    }, true);
    requestAnimationFrame(() => alphaChart && alphaChart.resize());
  }

  function renderAlpha158Dd(pf) {
    const el = $("#a158-dd-chart");
    if (!pf || !pf.drawdown || !pf.drawdown.length) return;
    if (!alphaDdChart) alphaDdChart = echarts.init(el);
    const dates = pf.dates.map(fmtDate);
    alphaDdChart.setOption({
      animationDuration: 400,
      tooltip: { trigger: "axis", valueFormatter: (v) => Number(v).toFixed(2) + "%" },
      grid: { left: 14, right: 20, top: 30, bottom: 36, containLabel: true },
      xAxis: { type: "category", data: dates, boundaryGap: false, axisLabel: { color: "#6b7280", fontSize: 11 }, axisLine: { lineStyle: { color: "#e5e7eb" } } },
      yAxis: { type: "value", axisLabel: { color: "#6b7280", formatter: "{value}%" }, splitLine: { lineStyle: { color: "#f1f5f9" } } },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 8 }],
      series: [{ name: "回撤", type: "line", data: pf.drawdown, showSymbol: false, smooth: true, lineStyle: { width: 1.6, color: "#ea580c" }, itemStyle: { color: "#ea580c" }, areaStyle: { color: "rgba(234,88,12,0.25)" } }],
    }, true);
    requestAnimationFrame(() => alphaDdChart && alphaDdChart.resize());
  }

  // ---------- 组合回测 (买入持有 + 区间交易, 区间交易自动估算最优买卖/止损价) ----------
  let ptfPoolItems = [];
  let ptfPoolOverride = null;
  let ptfStockItems = [];
  let ptfSort = { key: "band_ret", order: "desc" };

  async function loadPtfPool() {
    if (!window.CaiBaoAuth || !CaiBaoAuth.isLoggedIn()) return;
    if (ptfPoolOverride) { ptfPoolItems = ptfPoolOverride.items; renderPtfPool(); return; }
    try {
      const res = await fetch("/api/my_stocks");
      if (!res.ok) throw new Error("获取我的股票失败");
      const data = await res.json();
      ptfPoolItems = data.items || [];
      renderPtfPool();
    } catch (err) {
      ptfPoolBox.innerHTML = `<span class="alpha-pool-loading">${err.message || "加载失败"}</span>`;
    }
  }

  // 策略 Hub 结果 → 组合回测股票池 (支持 A股/港股/ETF, 区间交易参数自动估算)
  function runPortfolioWithStrategy(stocks, title) {
    const list = (stocks || []).filter((s) => s && s.ts_code);
    if (!list.length) { alert("该策略结果中没有公司可回测"); return; }
    ptfPoolOverride = { items: list, title: title || "策略" };
    ptfPoolItems = list;
    renderPtfPool();
    const tab = document.querySelector('.tab[data-tab="backtest"]');
    if (tab) tab.click();
    const seg = document.querySelector('#seg-backtest .seg-btn[data-panel="ptf"]');
    if (seg) seg.click();   // 切到组合回测面板 (loadPtfPool 因 override 存在而保留策略池)
    const hint = $("#ptf-hint");
    if (hint) hint.textContent = `已从「${ptfPoolOverride.title}」导入 ${list.length} 只 · 可调整参数后点击「运行组合回测」`;
    const sec = document.getElementById("tab-backtest");
    if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderPtfPool() {
    const label = $("#ptf-pool-label");
    if (ptfPoolOverride) {
      if (label) label.textContent = `股票池 (${ptfPoolOverride.title || "策略"})`;
      let html = `<div class="alpha-pool-source">已导入策略公司 ${ptfPoolOverride.items.length} 只 · ` +
        `<button type="button" class="strat-op" id="ptf-pool-reset">用回我的自选股</button></div>`;
      html += ptfPoolOverride.items.map((it) => `
        <label class="alpha-pool-item" title="${it.ts_code}">
          <input type="checkbox" value="${it.ts_code}" checked />
          <span>${it.name || it.ts_code}</span><em>${it.ts_code}</em>
        </label>`).join("");
      ptfPoolBox.innerHTML = html;
      const reset = $("#ptf-pool-reset");
      if (reset) reset.onclick = () => { ptfPoolOverride = null; loadPtfPool(); };
      return;
    }
    if (label) label.textContent = "股票池 (我的股票)";
    if (!ptfPoolItems.length) {
      ptfPoolBox.innerHTML = `<span class="alpha-pool-loading">暂无自选股, 请先添加或在策略 Hub 点「组合回测」导入</span>`;
      return;
    }
    ptfPoolBox.innerHTML = ptfPoolItems.map((it) => `
      <label class="alpha-pool-item" title="${it.ts_code}">
        <input type="checkbox" value="${it.ts_code}" checked />
        <span>${it.name || it.ts_code}</span><em>${it.ts_code}</em>
      </label>`).join("");
  }

  ptfForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const symbols = Array.from(ptfPoolBox.querySelectorAll("input[type=checkbox]:checked"))
      .map((c) => c.value);
    if (!symbols.length) {
      ptfError.textContent = "请至少选择一只股票。";
      ptfError.classList.remove("hidden");
      return;
    }
    const objMap = { balanced: "综合平衡", return: "收益优先", annual: "年化优先", sharpe: "夏普优先", drawdown: "回撤最小", calmar: "卡玛优先" };
    const objective = $("#ptf_objective").value;
    const payload = {
      symbols,
      start_date: $("#ptf_start").value.trim() || "20170101",
      end_date: $("#ptf_end").value.trim() || "",
      initial_capital: parseFloat($("#ptf_capital").value) || 100000,
      min_sharpe: parseFloat($("#ptf_min_sharpe").value) || 0,
      objective,
      max_trades: parseInt($("#ptf_max_trades").value, 10) || 100,
    };
    ptfError.classList.add("hidden");
    ptfResult.classList.add("hidden");
    ptfBtn.disabled = true;
    ptfLoading.classList.remove("hidden");
    $("#ptf-hint").textContent =
      `${symbols.length} 只 · ${payload.start_date} ~ ${payload.end_date || "最新"} · 夏普≥${payload.min_sharpe} · ${objMap[objective] || objective} · 交易≤${payload.max_trades}`;
    try {
      const res = await fetch("/api/portfolio/backtest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "组合回测失败");
      renderPtf(data);
    } catch (err) {
      ptfError.textContent = err.message || "请求失败, 请检查后端服务。";
      ptfError.classList.remove("hidden");
    } finally {
      ptfLoading.classList.add("hidden");
      ptfBtn.disabled = false;
    }
  });

  function renderPtf(data) {
    const { portfolio, stocks = [] } = data;
    const okStocks = stocks.filter((s) => s && s.ok);
    ptfResult.classList.remove("hidden");
    if (!okStocks.length) {
      $("#ptf-result-sub").textContent = "无可用回测结果";
      $("#ptf-metrics").innerHTML = `
        <div class="metric"><div class="m-label">回测结果</div>
          <div class="m-value" style="font-size:16px;color:#9ca3af">无股票可回测</div>
          <div class="m-sub">${(stocks || []).map((s) => `${s.name}(${s.reason || "失败"})`).join("；") || "请检查股票数据"}</div></div>`;
      $("#ptf-table tbody").innerHTML = `<tr><td colspan="11" style="text-align:center;color:#9ca3af;padding:24px">无回测结果</td></tr>`;
      ptfResult.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const pf = portfolio || {};
    const bm = pf.band_metrics || {};
    const hm = pf.buyhold_metrics || {};
    $("#ptf-result-sub").textContent =
      `${(data.params && data.params.start_date) || "—"} ~ ${(data.params && data.params.end_date) || "最新"} · ${okStocks.length} 只 · 等权 · 夏普≥${(data.params && data.params.min_sharpe) != null ? data.params.min_sharpe : "—"}`;
    $("#ptf-metrics").innerHTML = `
      <div class="metric ${cls(bm.total_return || 0)}"><div class="m-label">区间交易 · 总收益率</div>
        <div class="m-value">${fmtPct(bm.total_return || 0)}</div>
        <div class="m-sub">年化 ${fmtPct(bm.annual_return || 0)} · 期末 ¥${fmtNum(bm.final_value || 0)}</div></div>
      <div class="metric ${cls(bm.max_drawdown || 0)}"><div class="m-label">区间交易 · 最大回撤</div>
        <div class="m-value">${(bm.max_drawdown || 0).toFixed(2)}%</div>
        <div class="m-sub">夏普 ${(bm.sharpe || 0).toFixed(2)} · 卡玛 ${(bm.calmar || 0).toFixed(2)}</div></div>
      <div class="metric ${cls(hm.total_return || 0)}"><div class="m-label">买入持有 · 总收益率</div>
        <div class="m-value">${fmtPct(hm.total_return || 0)}</div>
        <div class="m-sub">年化 ${fmtPct(hm.annual_return || 0)} · 夏普 ${(hm.sharpe || 0).toFixed(2)}</div></div>
      <div class="metric ${cls((bm.total_return || 0) - (hm.total_return || 0))}"><div class="m-label">超额收益 (区间-持有)</div>
        <div class="m-value">${fmtPct((bm.total_return || 0) - (hm.total_return || 0))}</div>
        <div class="m-sub">回撤 ${(bm.max_drawdown || 0).toFixed(2)}% vs ${(hm.max_drawdown || 0).toFixed(2)}%</div></div>`;

    renderPtfChart(pf);
    ptfStockItems = okStocks;
    _renderPtfTable();
    ptfResult.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function _ptfVal(it, key) {
    const b = it.band || {}, p = it.params || {}, bh = it.buyhold || {};
    switch (key) {
      case "name": return it.name;
      case "ts_code": return it.ts_code;
      case "buy_price": return p.buy_price;
      case "sell_price": return p.sell_price;
      case "stop_price": return p.stop_price;
      case "band_ret": return b.total_return;
      case "band_sharpe": return b.sharpe;
      case "band_dd": return b.max_drawdown;
      case "trades": return it.trades_count;
      case "bh_ret": return bh.total_return;
      default: return null;
    }
  }

  function _renderPtfTable() {
    const arr = ptfStockItems.slice();
    const k = ptfSort.key;
    const dir = ptfSort.order === "asc" ? 1 : -1;
    arr.sort((a, b) => {
      const va = _ptfVal(a, k), vb = _ptfVal(b, k);
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
      return String(va == null ? "" : va).localeCompare(String(vb == null ? "" : vb), "zh") * dir;
    });
    const body = arr.map((it) => {
      const p = it.params || {}, b = it.band || {}, bh = it.buyhold || {};
      return `<tr>
        <td><a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(it.ts_code)}" title="查看详情">${it.name || it.ts_code}</a></td>
        <td>${it.ts_code}</td>
        <td class="num">${p.buy_price == null ? "—" : Number(p.buy_price).toFixed(2)}</td>
        <td class="num">${p.sell_price == null ? "—" : Number(p.sell_price).toFixed(2)}</td>
        <td class="num">${p.stop_price == null ? "—" : Number(p.stop_price).toFixed(2)}</td>
        <td class="num ${cls(b.total_return || 0)}">${fmtPct(b.total_return || 0)}</td>
        <td class="num">${(b.sharpe || 0).toFixed(2)}</td>
        <td class="num ${cls(-(b.max_drawdown || 0))}">${(b.max_drawdown || 0).toFixed(2)}%</td>
        <td class="num">${it.trades_count || 0}</td>
        <td class="num ${cls(bh.total_return || 0)}">${fmtPct(bh.total_return || 0)}</td>
        <td>${it.range ? (it.range.start + "~" + it.range.end) : "—"}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="11" style="text-align:center;color:#9ca3af;padding:24px">无可回测的股票</td></tr>`;
    $("#ptf-table tbody").innerHTML = body;
    document.querySelectorAll("#ptf-table thead th.sortable").forEach((th) => {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      arrow.textContent = (th.dataset.sort === ptfSort.key) ? (ptfSort.order === "desc" ? " ▼" : " ▲") : "";
      th.classList.toggle("sorted", th.dataset.sort === ptfSort.key);
    });
  }

  document.querySelectorAll("#ptf-table thead th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const by = th.dataset.sort;
      if (ptfSort.key === by) ptfSort.order = ptfSort.order === "desc" ? "asc" : "desc";
      else { ptfSort.key = by; ptfSort.order = "desc"; }
      _renderPtfTable();
    });
  });

  function renderPtfChart(pf) {
    const el = $("#ptf-chart");
    if (!pf || !pf.dates || !pf.dates.length) return;
    if (!ptfChart) ptfChart = echarts.init(el);
    const dates = pf.dates.map(fmtDate);
    ptfChart.setOption({
      animationDuration: 400,
      tooltip: { trigger: "axis", valueFormatter: (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%" },
      legend: { top: 4, textStyle: { fontSize: 13 } },
      grid: { left: 14, right: 20, top: 40, bottom: 36, containLabel: true },
      xAxis: { type: "category", data: dates, boundaryGap: false, axisLabel: { color: "#6b7280", fontSize: 11 }, axisLine: { lineStyle: { color: "#e5e7eb" } } },
      yAxis: { type: "value", axisLabel: { color: "#6b7280", formatter: "{value}%" }, splitLine: { lineStyle: { color: "#f1f5f9" } } },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 8 }],
      series: [
        { name: "区间交易", type: "line", data: pf.band, showSymbol: false, smooth: true, lineStyle: { width: 2.4, color: "#e65050" }, itemStyle: { color: "#e65050" }, areaStyle: { opacity: 0.07 } },
        { name: "买入持有", type: "line", data: pf.buyhold, showSymbol: false, smooth: true, lineStyle: { width: 2.2, color: "#4f46e5" }, itemStyle: { color: "#4f46e5" }, areaStyle: { opacity: 0.04 } },
      ],
    }, true);
    requestAnimationFrame(() => ptfChart && ptfChart.resize());
  }

  // 窗口尺寸变化时, 防抖自适应图表
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (chart) chart.resize();
      if (quoteChart) quoteChart.resize();
      if (bandChart) bandChart.resize();
      if (alphaChart) alphaChart.resize();
      if (alphaDdChart) alphaDdChart.resize();
      if (ptfChart) ptfChart.resize();
    }, 150);
  });

  // ---------- 用户认证: 未登录跳转独立登录页, 登录后展示 Tab 并加载自选股 ----------
  if (window.CaiBaoAuth) {
    CaiBaoAuth.onAuthChange(() => {
      if (CaiBaoAuth.isLoggedIn()) { loadMyStocks(); loadStrategies(); loadIdeas(); loadAlpha158Pool(); }
      else location.replace("/static/login.html");
    });
    CaiBaoAuth.init().then(() => {
      if (CaiBaoAuth.isLoggedIn()) {
        document.body.classList.remove("auth-loading");  // 已登录, 显示 Tab 与内容
        loadMyStocks();
        loadStrategies();
        loadIdeas();
        loadAlpha158Pool();
      } else {
        location.replace("/static/login.html");          // 未登录跳登录页 (不显示 Tab)
      }
    });
  } else {
    document.body.classList.remove("auth-loading");
    loadMyStocks();
  }

  checkHealth();
})();
