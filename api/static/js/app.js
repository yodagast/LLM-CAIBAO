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

  let chart = null;
  let quoteChart = null;
  let bandChart = null;
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

  // ---------- 股票联想 ----------
  stockInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const kw = stockInput.value.trim();
    if (!kw) { stockList.innerHTML = ""; tsTip.textContent = ""; return; }
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
  fillYearSelect($("#rlv_year"), 2025);

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
  bandInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const kw = bandInput.value.trim();
    if (!kw) { stockList.innerHTML = ""; bandTip.textContent = ""; return; }
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
      // 切换后重算图表尺寸 (隐藏容器尺寸为 0)
      setTimeout(() => {
        if (chart) chart.resize();
        if (quoteChart) quoteChart.resize();
        if (bandChart) bandChart.resize();
      }, 80);
    });
  });

  // ---------- 基本面选股 (ROE 杜邦拆分) ----------
  const SCREEN_SORT_LABELS = {
    year: "年份", close: "最近价", roe: "ROE", net_margin: "净利润率",
    assets_turn: "总资产周转率", equity_multiplier: "权益乘数", gross_margin: "毛利率",
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
    const roe = parseFloat($("#sc_f_roe").value);
    if (!isNaN(roe)) filters.roe = { min: roe };
    const debt = parseFloat($("#sc_f_debt").value);
    if (!isNaN(debt)) filters.debt_to_assets = { max: debt };

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
        screenSort.order = "desc";
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
    $("#screen-sub").textContent =
      `按 ${sortLabel} ${orderLabel}${screenFilterLabel(payload.filters)} · 数据来自 PostgreSQL${synced}`;

    const body = items.map((it) => `
      <tr>
        <td class="num">${it.year}</td>
        <td>${it.ts_code}</td>
        <td><a class="stock-link" href="/static/stock_detail.html?code=${encodeURIComponent(it.ts_code)}" target="_blank" title="查看详情">${it.name}</a></td>
        <td class="num ${cls(it.roe || 0)}">${fmtPctVal(it.roe)}</td>
        <td class="num ${cls(it.net_margin || 0)}">${fmtPctVal(it.net_margin)}</td>
        <td class="num">${it.assets_turn == null ? "—" : Number(it.assets_turn).toFixed(2)}</td>
        <td class="num">${it.equity_multiplier == null ? "—" : Number(it.equity_multiplier).toFixed(2)}</td>
        <td class="num ${cls(it.gross_margin || 0)}">${fmtPctVal(it.gross_margin)}</td>
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
    dividend_yield: "股息率", volatility: "波动率", div_per_share: "每股分红",
    free_cashflow: "自由现金流", eps: "每股收益", payout_ratio: "分红率",
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
  const rlvSort = { by: "dividend_yield", order: "desc" };

  // 从表单构造红利低波请求 (多年份数组 + 筛选条件 + 当前排序)
  function buildRlvPayload() {
    const years = [parseInt($("#rlv_year").value, 10)];

    const filters = {};
    const fNum = (id) => { const v = parseFloat($(id).value); return isNaN(v) ? null : v; };
    const dy = fNum("#rlv_f_dy"); if (dy !== null) filters.dividend_yield = { min: dy };
    const vol = fNum("#rlv_f_vol"); if (vol !== null) filters.volatility = { max: vol };
    const div = fNum("#rlv_f_div"); if (div !== null) filters.div_per_share = { min: div };
    const roe = fNum("#rlv_f_roe"); if (roe !== null) filters.roe = { min: roe };
    const debt = fNum("#rlv_f_debt"); if (debt !== null) filters.debt_to_assets = { max: debt };
    const payout = fNum("#rlv_f_payout"); if (payout !== null) filters.payout_ratio = { min: payout };

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
        rlvSort.order = "desc";
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
    $("#rlv-sub").textContent =
      `按 ${sortLabel} ${orderLabel}${filterLabel(payload.filters)} · 数据来自 PostgreSQL${synced}`;

    const body = items.map((it) => `
      <tr>
        <td class="num">${it.year}</td>
        <td>${it.ts_code}</td>
        <td><b>${it.name}</b></td>
        <td>${it.industry || "—"}</td>
        <td class="num ${cls(it.dividend_yield || 0)}">${fmtPctVal(it.dividend_yield)}</td>
        <td class="num">${fmtPctVal(it.volatility)}</td>
        <td class="num">${it.div_per_share == null ? "—" : Number(it.div_per_share).toFixed(2) + " 元"}</td>
        <td class="num">${fmtYi(it.free_cashflow)}</td>
        <td class="num">${it.eps == null ? "—" : Number(it.eps).toFixed(2)}</td>
        <td class="num">${fmtPctVal(it.payout_ratio)}</td>
        <td class="num ${cls(it.roe || 0)}">${fmtPctVal(it.roe)}</td>
        <td class="num">${fmtPctVal(it.debt_to_assets)}</td>
      </tr>`).join("");
    $("#rlv-table tbody").innerHTML = body ||
      `<tr><td colspan="12" style="text-align:center;color:#9ca3af;padding:24px">无符合条件的数据</td></tr>`;
    updateRlvSortArrows();
    rlvResult.scrollIntoView({ behavior: "smooth", block: "start" });
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

    $("#band-result-title").textContent = `${info.name} (${info.ts_code}) · 区间交易最优参数`;
    const achievedTxt = search.achieved ? "✅ 夏普达标" : "⚠️ 未达目标夏普 (已取折中)";
    $("#band-result-sub").textContent =
      `${fmtDate(range.start)} ~ ${fmtDate(range.end)} · ${range.bars} 个交易日 · ` +
      `目标 ${search.objective_label || "收益优先"} · ` +
      `搜索 ${search.tried} 组参数 · 目标夏普 ≥ ${search.min_sharpe} · ${achievedTxt}`;
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

    renderBandChart(band, baseline);
    bandResult.scrollIntoView({ behavior: "smooth", block: "start" });
  }

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

  // 窗口尺寸变化时, 防抖自适应图表
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (chart) chart.resize();
      if (quoteChart) quoteChart.resize();
      if (bandChart) bandChart.resize();
    }, 150);
  });

  checkHealth();
})();
