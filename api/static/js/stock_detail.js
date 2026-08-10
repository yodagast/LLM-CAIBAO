/* 股票详情页逻辑: 读取 ?code=, 拉取详情并渲染 K线 + 数据卡片 */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  let chart = null;
  let isHk = false;   // 港股 (单位用港元, 无前复权)

  function fmtNum(v, d = 2) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(d);
  }
  // 万股 -> 亿股; 万元 -> 亿元
  function fmtYi(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    const yi = Number(v) / 10000;
    if (Math.abs(yi) >= 100) return yi.toFixed(0) + "亿";
    if (Math.abs(yi) >= 1) return yi.toFixed(2) + "亿";
    return Number(v).toLocaleString("zh-CN");
  }
  function fmtDate(ymd) {
    if (!ymd || ymd.length !== 8) return ymd;
    return `${ymd.slice(0, 4)}-${ymd.slice(4, 6)}-${ymd.slice(6, 8)}`;
  }
  function cls(v) { return v >= 0 ? "pos" : "neg"; }

  function ma(arr, n) {
    const out = [];
    for (let i = 0; i < arr.length; i++) {
      if (i < n - 1) { out.push("-"); continue; }
      let s = 0;
      for (let j = i - n + 1; j <= i; j++) s += arr[j];
      out.push(+(s / n).toFixed(3));
    }
    return out;
  }

  async function checkHealth() {
    try {
      const r = await fetch("/api/health");
      $("#health-text").textContent = r.ok ? "服务已连接" : "服务异常";
    } catch (e) {
      $("#health-text").textContent = "后端服务未启动";
    }
  }

  function renderKline(bars, currency = "元") {
    const el = $("#kchart");
    if (!chart) chart = echarts.init(el);
    const dates = bars.map((b) => fmtDate(b.date));
    const kData = bars.map((b) => [b.open, b.close, b.low, b.high]);
    const closes = bars.map((b) => b.close);
    const vols = bars.map((b) => b.vol);
    const volColors = bars.map((b) =>
      b.close >= b.open ? "rgba(230, 80, 80, .7)" : "rgba(38, 166, 154, .7)");

    const total = bars.length;
    // 默认显示最近 250 根 (约 1 年), 可缩放/拖拽查看全部历史 (最多 20 年)
    const win = 250;
    const startPct = total > win ? Math.max(0, ((total - win) / total) * 100) : 0;

    chart.setOption({
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (params) => {
          const i = params[0].dataIndex;
          const b = bars[i];
          let html = `<b>${fmtDate(b.date)}</b><br/>`;
          html += `开盘 ${b.open.toFixed(2)}　收盘 <b>${b.close.toFixed(2)}</b><br/>`;
          html += `最高 ${b.high.toFixed(2)}　最低 ${b.low.toFixed(2)}<br/>`;
          const ampTip = b.pre_close && b.pre_close > 0 ? ((b.high - b.low) / b.pre_close * 100).toFixed(2) + "%" : "—";
          const trTip = b.turnover_rate != null ? b.turnover_rate.toFixed(2) + "%" : "—";
          const volTip = b.vol != null ? (b.vol >= 10000 ? (b.vol / 10000).toFixed(2) + "万" : b.vol.toFixed(0)) + (isHk ? "股" : "手") : "—";
          const amtTip = b.amount != null ? (b.amount >= 100000 ? (b.amount / 100000).toFixed(2) + "亿" : b.amount.toFixed(0) + "千") + currency : "—";
          html += `涨跌 <b style="color:${b.pct_chg >= 0 ? "#e65050" : "#26a69a"}">${b.pct_chg >= 0 ? "+" : ""}${b.pct_chg.toFixed(2)}%</b>　振幅 <b>${ampTip}</b><br/>`;
          html += `成交量 ${volTip}　成交额 ${amtTip}<br/>`;
          html += `换手率 ${trTip}`;
          return html;
        },
      },
      legend: { top: 4, data: ["K线", "MA5", "MA20", "成交量"], textStyle: { fontSize: 12 } },
      grid: [
        { left: 62, right: 20, top: 36, height: "58%" },
        { left: 62, right: 20, top: "76%", height: "13%" },
      ],
      xAxis: [
        { type: "category", data: dates, boundaryGap: true,
          axisLine: { lineStyle: { color: "#e5e7eb" } },
          axisLabel: { color: "#9ca3af", fontSize: 10, hideOverlap: true, formatter: (v) => v.slice(5) } },
        { type: "category", gridIndex: 1, data: dates, boundaryGap: true,
          axisLabel: { show: false }, axisLine: { lineStyle: { color: "#e5e7eb" } } },
      ],
      yAxis: [
        { scale: true, axisLabel: { color: "#9ca3af", fontSize: 10 },
          splitLine: { lineStyle: { color: "#f1f5f9" } } },
        { gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, splitLine: { show: false } },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: startPct, end: 100, zoomOnMouseWheel: true, moveOnMouseMove: true },
        { type: "slider", xAxisIndex: [0, 1], top: "94%", height: 18, start: startPct, end: 100, showDetail: true },
      ],
      series: [
        { name: "K线", type: "candlestick", data: kData,
          itemStyle: { color: "#e65050", color0: "#26a69a", borderColor: "#e65050", borderColor0: "#26a69a" } },
        { name: "MA5", type: "line", data: ma(closes, 5), smooth: true, symbol: "none",
          lineStyle: { width: 1, color: "#f59e0b" } },
        { name: "MA20", type: "line", data: ma(closes, 20), smooth: true, symbol: "none",
          lineStyle: { width: 1, color: "#3b82f6" } },
        { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: vols,
          itemStyle: { color: (p) => volColors[p.dataIndex] || "#e65050" } },
      ],
    }, true);
  }

  // 渲染当日行情快照 (从 bars 中取一条)
  function renderQuoteSnap(b) {
    if (!b) return;
    $("#q-open").textContent = fmtNum(b.open);
    $("#q-high").textContent = fmtNum(b.high);
    $("#q-low").textContent = fmtNum(b.low);
    $("#q-close").textContent = fmtNum(b.close);
    $("#q-pre").textContent = fmtNum(b.pre_close);
    // 涨跌额
    const chgEl = $("#q-chg");
    chgEl.textContent = b.change == null ? "—" : (b.change >= 0 ? "+" : "") + fmtNum(b.change);
    chgEl.className = "d-value " + (b.change != null ? cls(b.change) : "");
    // 涨跌幅
    const pctEl = $("#q-pct");
    pctEl.textContent = b.pct_chg == null ? "—" : (b.pct_chg >= 0 ? "+" : "") + b.pct_chg.toFixed(2) + "%";
    pctEl.className = "d-value " + cls(b.pct_chg);
    // 振幅 (高-低)/昨收
    $("#q-amp").textContent = b.amplitude == null ? "—" : b.amplitude.toFixed(2) + "%";
    // 成交量 (手 -> 万手)
    $("#q-vol").textContent = b.vol == null ? "—"
      : (b.vol >= 10000 ? (b.vol / 10000).toFixed(2) + " 万" : fmtNum(b.vol));
    // 成交额 (千元 -> 元 -> 亿元)
    $("#q-amt").textContent = b.amount == null ? "—"
      : (b.amount >= 100000 ? (b.amount / 100000).toFixed(2) + " 亿" : fmtNum(b.amount) + " 千");
    // 换手率
    $("#q-turnover").textContent = b.turnover_rate == null ? "—" : b.turnover_rate.toFixed(2) + "%";
    // 盘后成交量 (tushare 暂无数据)
    $("#q-after").textContent = "—";
    // 顶部价格同步为选中日期
    $("#d-price").textContent = fmtNum(b.close);
    $("#d-chg").textContent = b.pct_chg == null ? "" : (b.pct_chg >= 0 ? "+" : "") + b.pct_chg.toFixed(2) + "%";
    $("#d-chg").className = "chg " + (b.pct_chg != null ? cls(b.pct_chg) : "");
    $("#d-date").textContent = fmtDate(b.date);
  }

  // 日期选择器: 选择不同交易日查看行情快照
  function setupDatePicker(bars, data) {
    const el = $("#q-date");
    if (!bars.length) return;
    const minY = bars[0].date;
    const maxY = bars[bars.length - 1].date;
    el.min = fmtDate(minY);
    el.max = fmtDate(maxY);
    el.value = fmtDate(data.last_date);
    $("#q-date-range").textContent = `可选 ${fmtDate(minY)} ~ ${fmtDate(maxY)}`;
    el.onchange = () => {
      const sel = el.value.replace(/-/g, "");
      const idx = bars.findIndex((b) => b.date === sel);
      if (idx < 0) return;
      renderQuoteSnap(bars[idx]);
      // K 线定位到选中日期附近 (显示约 80 根)
      if (chart) {
        const total = bars.length;
        const win = 80;
        const startPct = Math.max(0, ((idx - win / 2) / total) * 100);
        const endPct = Math.min(100, ((idx + win / 2) / total) * 100);
        chart.dispatchAction({ type: "dataZoom", start: startPct, end: endPct });
      }
    };
  }

  function render(data) {
    const { info } = data;
    const last = data.last_close;
    const prev = data.bars && data.bars.length > 1 ? data.bars[data.bars.length - 2].close : null;
    const chg = prev ? (last / prev - 1) * 100 : null;
    isHk = info.kind === "hk" || String(info.ts_code).endsWith(".HK");
    const currency = isHk ? "港元" : "元";

    $("#d-name").textContent = info.name;
    $("#d-code-line").textContent =
      `${info.ts_code} · ${info.industry || "—"} · ${info.kind === "fund" ? "基金/ETF" : (isHk ? "港股" : "股票")}`;
    $("#d-price").textContent = fmtNum(last);
    $("#d-chg").textContent = chg === null ? "" : `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`;
    $("#d-chg").className = "chg " + (chg !== null ? cls(chg) : "");
    $("#d-date").textContent = fmtDate(data.last_date);

    $("#c-pb").textContent = fmtNum(data.pb);
    $("#c-pe").textContent = fmtNum(data.pe_ttm ?? data.pe);
    $("#c-div").textContent = data.div_per_share == null ? "—" : fmtNum(data.div_per_share) + ` ${currency}`;
    $("#c-div-end").textContent = data.dividend_end ? `分红年度 ${data.dividend_end.slice(0, 4)}` : "";
    $("#c-dy").textContent = data.dividend_yield == null ? "—" : fmtNum(data.dividend_yield) + "%";
    $("#c-52l").textContent = fmtNum(data.week52_low);
    $("#c-52h").textContent = fmtNum(data.week52_high);
    $("#c-share").textContent = fmtYi(data.total_share) + " 股";
    $("#c-float-share").textContent = data.float_share ? `流通 ${fmtYi(data.float_share)} 股` : "";
    $("#c-mv").textContent = fmtYi(data.total_mv) + ` ${currency}`;
    $("#c-circ-mv").textContent = data.circ_mv ? `流通市值 ${fmtYi(data.circ_mv)} ${currency}` : "";

    // 港股: 行情卡成交量单位 手→股, 成交额单位 元→港元
    if (isHk) {
      const setSub = (id, txt) => {
        const el = document.querySelector(id);
        if (el) el.parentElement.querySelector(".d-sub").textContent = txt;
      };
      setSub("#q-vol", "股");
      setSub("#q-amt", "港元");
    }

    if (data.bars && data.bars.length) {
      renderKline(data.bars, currency);
      setupDatePicker(data.bars, data);
      renderQuoteSnap(data.bars[data.bars.length - 1]);
    }
  }

  // 加入/移除我的股票 (自选股): 已加入则点击移除, 未加入则点击加入
  function bindAddMyStock() {
    const btn = $("#add-my-btn");
    if (!btn) return;
    let inList = false;

    function setAddedState() {
      btn.textContent = "✓ 已加入 · 点击移除";
      btn.classList.add("added");
    }
    function setNotAddedState() {
      btn.textContent = "⭐ 加入我的股票";
      btn.classList.remove("added");
    }

    // 初始查询该股票是否已在自选股, 决定按钮初始状态 (需登录)
    (async () => {
      if (!window.CaiBaoAuth || !CaiBaoAuth.isLoggedIn()) return;
      try {
        const r = await fetch(`/api/my_stocks/contains/${encodeURIComponent(curCode)}`);
        const data = await r.json();
        if (data && data.in_list) {
          inList = true;
          setAddedState();
        }
      } catch (e) { /* 忽略 */ }
    })();

    btn.addEventListener("click", async () => {
      if (btn.disabled) return;
      // 未登录: 弹出登录框, 不做操作
      if (!window.CaiBaoAuth || !CaiBaoAuth.requireLogin()) return;
      btn.disabled = true;
      try {
        const url = inList ? "/api/my_stocks/remove" : "/api/my_stocks/add";
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ts_code: curCode }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "操作失败");
        inList = !inList;
        if (inList) setAddedState(); else setNotAddedState();
      } catch (err) {
        btn.textContent = "✗ 操作失败";
        setTimeout(() => {
          btn.textContent = inList ? "✓ 已加入 · 点击移除" : "⭐ 加入我的股票";
        }, 2000);
      } finally {
        btn.disabled = false;
      }
    });
  }

  async function init() {
    const params = new URLSearchParams(location.search);
    curCode = (params.get("code") || "").trim();
    if (!curCode) {
      $("#error").textContent = "缺少 code 参数, 请从基本面选股结果点击进入。";
      $("#loading").classList.add("hidden");
      $("#error").classList.remove("hidden");
      return;
    }
    try {
      const res = await fetch(`/api/stock/detail/${encodeURIComponent(curCode)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "加载失败");
      $("#loading").classList.add("hidden");
      $("#content").classList.remove("hidden");
      render(data);
      bindAddMyStock();
      bindKlineToolbar();
      if (isHk) klineAdj = "";   // 港股无可靠前复权, 默认不复权
      reloadKline();   // 初始按默认(日线+前复权)加载 K 线
      requestAnimationFrame(() => chart && chart.resize());
    } catch (err) {
      $("#loading").classList.add("hidden");
      $("#error").textContent = err.message || "加载失败";
      $("#error").classList.remove("hidden");
    }
  }

  // ---------- K 线周期/复权切换 ----------
  let curCode = "";
  let klineFreq = "D";
  let klineAdj = "qfq";   // 默认前复权

  async function reloadKline() {
    const loading = $("#kline-loading");
    loading.classList.remove("hidden");
    try {
      const r = await fetch(`/api/stock/kline/${encodeURIComponent(curCode)}` +
        `?freq=${klineFreq}&adj=${encodeURIComponent(klineAdj)}`);
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || "加载K线失败");
      renderKline(data.bars || []);
    } catch (err) {
      // K 线加载失败时保留原图, 仅提示
      $("#kline-loading").textContent = err.message || "加载失败";
    } finally {
      loading.classList.add("hidden");
      setTimeout(() => { $("#kline-loading").textContent = "加载中…"; }, 1500);
    }
  }

  function bindKlineToolbar() {
    document.querySelectorAll("#k-freq .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#k-freq .seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
        klineFreq = btn.dataset.freq;
        reloadKline();
      });
    });
    document.querySelectorAll("#k-adj .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#k-adj .seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
        klineAdj = btn.dataset.adj;
        reloadKline();
      });
    });
  }

  window.addEventListener("resize", () => { if (chart) chart.resize(); });
  checkHealth();
  if (window.CaiBaoAuth) {
    CaiBaoAuth.init().then(() => { init(); });
  } else {
    init();
  }
})();
