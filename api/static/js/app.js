/* 单股三策略回测系统 - 前端逻辑 */
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

  let chart = null;
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
          .map((it) => `<option value="${it.ts_code}">${it.name} (${it.symbol})</option>`)
          .join("");
      } catch (e) { /* 忽略联想失败 */ }
    }, 300);
  });

  // 输入代码后尝试解析显示名称
  stockInput.addEventListener("change", async () => {
    const code = stockInput.value.trim();
    if (!code) { tsTip.textContent = ""; return; }
    try {
      const r = await fetch(`/api/stock/${encodeURIComponent(code)}`);
      if (r.ok) {
        const info = await r.json();
        tsTip.classList.add("ok");
        tsTip.classList.remove("err");
        tsTip.textContent = `✓ ${info.name} · ${info.ts_code} · ${info.kind === "fund" ? "基金/ETF" : "股票"}`;
      } else {
        const err = await r.json();
        tsTip.classList.add("err");
        tsTip.classList.remove("ok");
        tsTip.textContent = `✗ ${err.detail || "未找到该代码"}`;
      }
    } catch (e) {
      tsTip.textContent = "";
    }
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
    } catch (err) {
      showError(err.message || "请求失败, 请检查后端服务。");
    } finally {
      loading.classList.add("hidden");
      runBtn.disabled = false;
    }
  });

  // ---------- 渲染 ----------
  function renderResult(data) {
    const { info, params, range, strategies } = data;
    resultBox.classList.remove("hidden");

    $("#result-title").textContent = `${info.name} (${info.ts_code}) · 三策略回测`;
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
    const tags = { "买入持有": "bh", "区间交易": "band", "低价买入": "low" };
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
    const colors = ["#4f46e5", "#ea580c", "#059669"];
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
    window.addEventListener("resize", () => chart && chart.resize());
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
    hideError();
  });

  checkHealth();
})();
