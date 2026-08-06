"""财报分析服务: 下载年报 PDF + 文本提取 + 指标计算 + 分析报告生成。

工作流:
  1. download_reports : 从新浪财经抓取指定年份年报 PDF 保存到本地 (pdf/caibao/{名称}-{代码}/)
  2. extract_pdf_text  : 用 pdfplumber 提取 PDF 文本
  3. collect_financials: 用 tushare 拉取资产负债表/利润表/现金流/财务指标/估值
  4. analyze_rule_based: 规则化分析 (基于 skill 框架的指标驱动判断, 无需 LLM)
  5. analyze_llm       : 若 .env 配置了 DEEPSEEK_API_KEY / OPENAI_API_KEY, 用 LLM 深度分析
  6. analyze           : 主入口, 下载→提取→分析→保存 markdown 报告

报告为 Markdown 格式 (对应 .claude/skills/caibao-skill/SKILL.md 的输出结构),
前端用 marked 渲染展示并支持下载。
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

from . import data_service, pg_service

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PDF_ROOT = PROJECT_ROOT / "pdf" / "caibao"
REPORT_DIR = PROJECT_ROOT / "reports"


def _load_env() -> None:
    """加载项目根目录 .env 到 os.environ (setdefault, 不覆盖已有环境变量)。"""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

# 新浪财经年报公告页面 URL 模板 (与 down_caibao.py 一致)
BASE_URL_TEMPLATE = ("https://money.finance.sina.com.cn/corp/go.php/"
                     "vCB_Bulletin/stockid/{stock_code}/page_type/ndbg.phtml")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------------------
# 1) 年报 PDF 下载 (精简自 down_caibao.py)
# ---------------------------------------------------------------------------

def _sina_pdf_links(stock_code: str, years: list[int]) -> list[tuple[int, str, str]]:
    """从新浪财经年报列表页抓取指定年份的 PDF 链接, 返回 [(year, url, title)]。"""
    url = BASE_URL_TEMPLATE.format(stock_code=stock_code)
    pdf_links: list[tuple[int, str, str]] = []
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30)
        resp.encoding = "gb2312"
        soup = BeautifulSoup(resp.text, "html.parser")

        def _abs(href: str) -> str:
            if href.startswith("/"):
                return "https://money.finance.sina.com.cn" + href
            return href if href.startswith("http") else "https://money.finance.sina.com.cn/" + href

        def _match(link):
            href = link.get("href", "")
            title = link.get_text(strip=True)
            # 新浪年报标题形如 "招商银行：2025年度报告", 需同时匹配 "年报" 与 "年度报告"
            if (".pdf" not in href.lower()
                    and "年报" not in title and "年度报告" not in title):
                return None
            m = re.search(r"(20\d{2})", title)
            if not m or int(m.group(1)) not in years:
                return None
            return int(m.group(1)), _abs(href), title

        for link in soup.find_all("a", href=True):
            hit = _match(link)
            if not hit:
                continue
            y, _url, title = hit
            # 同年份若有多个版本(新浪常同时列中文/英文版), 优先中文版(标题不含"英文")
            existing = next((i for i, p in enumerate(pdf_links) if p[0] == y), None)
            if existing is None:
                pdf_links.append(hit)
            elif "英文" in pdf_links[existing][2] and "英文" not in title:
                pdf_links[existing] = hit  # 用中文版替换英文版

        # 未直接命中 → 从公告详情页解析 PDF
        if not pdf_links:
            for table in soup.find_all("table"):
                for row in table.find_all("tr"):
                    for col in row.find_all("td"):
                        for a in col.find_all("a", href=True):
                            hit = _match(a)
                            if not hit:
                                continue
                            pdf_url = _detail_pdf_url(hit[1])
                            if pdf_url:
                                pdf_links.append((hit[0], pdf_url, hit[2]))
    except Exception as e:
        print(f"获取 {stock_code} 年报链接失败: {e}")
    return pdf_links


def _detail_pdf_url(detail_url: str) -> str:
    """从公告详情页解析 PDF 下载链接。"""
    try:
        resp = requests.get(detail_url, headers=_HEADERS, timeout=30)
        resp.encoding = "gb2312"
        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if ".pdf" in href.lower():
                return href if href.startswith("http") else "https://money.finance.sina.com.cn/" + href
    except Exception:
        pass
    return ""


def _download_pdf(url: str, save_path: Path) -> bool:
    """下载 PDF, 校验 %PDF 文件头, 失败自动重试一次 (带 Referer)。"""
    for attempt in range(2):
        try:
            hdrs = dict(_HEADERS)
            if attempt == 1:
                hdrs["Referer"] = "https://finance.sina.com.cn/"
            resp = requests.get(url, headers=hdrs, timeout=90, stream=True)
            if resp.status_code != 200:
                continue
            tmp = save_path.with_suffix(".part")
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            # 校验是否真 PDF
            head = tmp.read_bytes()[:1024]
            if b"%PDF" not in head:
                tmp.unlink(missing_ok=True)
                continue
            tmp.rename(save_path)
            return True
        except Exception as e:
            print(f"下载 PDF 失败 {url}: {e}")
            try:
                save_path.with_suffix(".part").unlink(missing_ok=True)
            except Exception:
                pass
    return False


def download_reports(ts_code: str, years: list[int], save_dir: Path | None = None) -> list[dict]:
    """下载指定年份年报 PDF, 返回 [{year, title, path}]。已存在的文件跳过。"""
    save_dir = save_dir or PDF_ROOT
    symbol = ts_code.split(".")[0]
    try:
        info = data_service.resolve_code(ts_code)
        name = info.get("name", symbol)
    except Exception:
        name = symbol

    stock_dir = save_dir / f"{name}-{symbol}"
    stock_dir.mkdir(parents=True, exist_ok=True)

    pdf_links = _sina_pdf_links(symbol, years)
    files: list[dict] = []
    for year, detail_url, title in pdf_links:
        safe_title = re.sub(r"[\\/:*?\"<>|]", "_", title)
        save_path = stock_dir / f"{symbol}_{year}_{safe_title}.pdf"
        if save_path.exists() and save_path.stat().st_size > 0:
            print(f"  [{year}] {title} - 已存在, 跳过")
            files.append({"year": year, "title": title, "path": str(save_path)})
            continue
        # pdf_url 是公告详情页, 需先解析真实 PDF 下载链接
        pdf_url = _detail_pdf_url(detail_url)
        if not pdf_url:
            print(f"  [{year}] {title} - 未找到 PDF 链接, 跳过")
            continue
        print(f"  [{year}] 下载: {title}")
        if not _download_pdf(pdf_url, save_path):
            print(f"  [{year}] 下载失败: {title}")
            continue
        time.sleep(1)
        files.append({"year": year, "title": title, "path": str(save_path)})
    return files


# ---------------------------------------------------------------------------
# 2) PDF 文本提取
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_path: str, max_chars: int = 200000) -> str:
    """用 pdfplumber 提取 PDF 全文, 截断到 max_chars 防止超长。"""
    chunks: list[str] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                chunks.append(text)
                if sum(len(c) for c in chunks) > max_chars:
                    break
    except Exception as e:
        print(f"提取 {pdf_path} 文本失败: {e}")
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# 3) tushare 财务指标收集 (年报口径)
# ---------------------------------------------------------------------------

def _fmt_yuan(v, unit: str = "亿") -> float:
    """元 → 亿/万, 缺失返回 NaN。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    if unit == "亿":
        return round(x / 1e8, 2)
    if unit == "万":
        return round(x / 1e4, 2)
    return round(x, 4)


def _year_rows(df: pd.DataFrame) -> dict:
    """把接口返回的 DataFrame 转成 {年份: 首行指标dict} (年报 end_date 1231)。"""
    if df is None or df.empty:
        return {}
    d = df.copy()
    if "end_date" in d.columns:
        d = d[d["end_date"].astype(str).str.endswith("1231")]
    d = d.sort_values("end_date")
    out: dict = {}
    for _, row in d.iterrows():
        y = int(str(row.get("end_date", ""))[:4])
        out[y] = {k: (None if pd.isna(v) else v)
                  for k, v in row.items() if k not in ("ts_code",)}
    return out


def collect_financials(ts_code: str, years: list[int]) -> dict:
    """收集每年财务指标 (tushare 年报口径), 返回 {year: {指标: 值}}。

    注意: balancesheet/income/cashflow 接口存在披露时滞, end_date 若截断到请求
    最大年份可能拿不到最新年报, 故 end_date 统一用今天日期, 再过滤请求年份。
    """
    pro = data_service._init_pro()
    start = f"{min(years)}0101"
    end = datetime.now().strftime("%Y%m%d")

    fina = _year_rows(pro.fina_indicator(ts_code=ts_code, start_date=start, end_date=end))
    bal = _year_rows(pro.balancesheet(ts_code=ts_code, start_date=start, end_date=end))
    inc = _year_rows(pro.income(ts_code=ts_code, start_date=start, end_date=end))
    cf = _year_rows(pro.cashflow(ts_code=ts_code, start_date=start, end_date=end))

    out: dict = {}
    for y in years:
        f, b, i, c = fina.get(y, {}), bal.get(y, {}), inc.get(y, {}), cf.get(y, {})
        # 跳过无任何披露数据的年份 (如当年年报未披露/超时间范围, 不生成占位行)
        if not (f or b or i or c):
            print(f"  [tushare] {ts_code} {y} 年财报未披露, 跳过")
            continue
        n_income = i.get("n_income_attr_p") or i.get("n_income")
        ocf = c.get("n_cashflow_act")
        # _fmt_yuan 默认 unit="亿" 用于金额(元→亿); 比率/周转/乘数用 unit="元" 直接保留
        out[y] = {
            "year": y,
            # 盈利
            "total_revenue_亿": _fmt_yuan(i.get("total_revenue")),
            "oper_cost_亿": _fmt_yuan(i.get("oper_cost")),
            "net_income_亿": _fmt_yuan(n_income),
            "gross_margin_%": _fmt_yuan(f.get("grossprofit_margin"), unit="元"),
            "net_margin_%": _fmt_yuan(f.get("netprofit_margin"), unit="元"),
            "roe_%": _fmt_yuan(f.get("roe"), unit="元"),
            "or_yoy_%": _fmt_yuan(f.get("or_yoy"), unit="元"),
            "netprofit_yoy_%": _fmt_yuan(f.get("netprofit_yoy"), unit="元"),
            # 费用率
            "sell_exp_亿": _fmt_yuan(i.get("sell_exp")),
            "admin_exp_亿": _fmt_yuan(i.get("admin_exp")),
            "fin_exp_亿": _fmt_yuan(i.get("fin_exp")),
            "rd_exp_亿": _fmt_yuan(i.get("rd_exp") or f.get("rd_exp")),
            # 资产结构
            "total_assets_亿": _fmt_yuan(b.get("total_assets")),
            "total_cur_assets_亿": _fmt_yuan(b.get("total_cur_assets")),
            "money_cap_亿": _fmt_yuan(b.get("money_cap")),
            "accounts_receiv_亿": _fmt_yuan(b.get("accounts_receiv")),
            "inventory_亿": _fmt_yuan(b.get("inventory")),
            "fixed_assets_亿": _fmt_yuan(b.get("fixed_assets")),
            "contract_liab_亿": _fmt_yuan(b.get("contract_liab")),
            "inv_assets_亿": _fmt_yuan(b.get("inv_assets")),
            # 负债与偿债
            "total_liab_亿": _fmt_yuan(b.get("total_liab")),
            "total_cur_liab_亿": _fmt_yuan(b.get("total_cur_liab")),
            "debt_to_assets_%": _fmt_yuan(f.get("debt_to_assets"), unit="元"),
            "current_ratio": _fmt_yuan(f.get("current_ratio"), unit="元"),
            "quick_ratio": _fmt_yuan(f.get("quick_ratio"), unit="元"),
            # 周转 (次/年 → 天数)
            "ar_turn": _fmt_yuan(f.get("ar_turn"), unit="元"),
            "inv_turn": _fmt_yuan(f.get("inv_turn"), unit="元"),
            "assets_turn": _fmt_yuan(f.get("assets_turn"), unit="元"),
            "equity_multiplier": _fmt_yuan(f.get("equity_multiplier"), unit="元"),
            # 现金流
            "ocf_亿": _fmt_yuan(ocf),
            "icf_亿": _fmt_yuan(c.get("n_cashflow_inv")),
            "fcf_亿": _fmt_yuan(c.get("n_cashflow_fnc")),
            "ocf_for_ratio": (float(ocf) if ocf is not None else float("nan")),
        }
        # 净现比 = 经营现金流 / 净利润
        try:
            ni = float(n_income) if n_income is not None else float("nan")
            oc = out[y]["ocf_for_ratio"]
            out[y]["净现比"] = round(oc / ni, 2) if ni and abs(ni) > 0 else float("nan")
        except Exception:
            out[y]["净现比"] = float("nan")
    return out


def _latest_valuation(ts_code: str) -> dict:
    """最新估值 (PE/PB/股息率/市值)。

    股息率用 daily_basic 的 dv_ttm (滚动12个月股息率): 它已包含一年多次分红
    (中期+末期等), 与自行计算的"最新年度全年分红/股价"一致; 而 dv_ratio 在
    部分公司会异常偏高 (如五粮液 dv_ratio≈11% vs 正确≈6.8%)。
    """
    pro = data_service._init_pro()
    try:
        df = pro.daily_basic(ts_code=ts_code,
                             start_date=(datetime.now() - pd.Timedelta(days=30)).strftime("%Y%m%d"),
                             end_date=datetime.now().strftime("%Y%m%d"),
                             fields="trade_date,close,pe_ttm,pb,dv_ratio,dv_ttm,total_mv,circ_mv")
        if df is None or df.empty:
            return {}
        row = df.sort_values("trade_date").iloc[-1]
        return {
            "close": round(float(row.get("close", 0) or 0), 2),
            "pe_ttm": round(float(row.get("pe_ttm", 0) or 0), 2),
            "pb": round(float(row.get("pb", 0) or 0), 2),
            "dv_ratio_%": round(float(row.get("dv_ttm", row.get("dv_ratio", 0)) or 0), 2),
            "total_mv_亿": round(float(row.get("total_mv", 0) or 0) / 10000, 2),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 4) 规则化分析 (无需 LLM)
# ---------------------------------------------------------------------------

def _trend(vals: list, reverse: bool = False) -> str:
    """给出一组数值的定性趋势 (升/降/波动/平稳)。"""
    valid = [v for v in vals if v == v]
    if len(valid) < 2:
        return "数据不足"
    first, last = valid[0], valid[-1]
    chg = (last / first - 1) * 100 if first else 0
    if reverse:
        chg = -chg
    if abs(chg) < 3:
        return "基本平稳"
    return f"{'上升' if chg > 0 else '下降'} {abs(chg):.0f}%"


def analyze_rule_based(info: dict, financials: dict, valuation: dict) -> str:
    """基于指标数据生成结构化 Markdown 财报分析报告 (对应 skill 框架)。"""
    years = sorted(financials.keys())
    y0, y1 = years[0], years[-1] if years else 0
    n = len(years)
    name = info.get("name", "")
    code = info.get("ts_code", "")

    def col(key, fmt="{:.2f}", default="—"):
        rows = []
        for y in years:
            v = financials[y].get(key)
            if v is None or v != v:
                rows.append(default)
            else:
                rows.append(fmt(v) if callable(fmt) else fmt.format(v))
        return " | ".join(rows)

    # 用于趋势判断的数值序列
    def series(key):
        return [financials[y].get(key) for y in years]

    L = []
    A = L.append
    A(f"# {name}（{code}）{y0}~{y1} 年财报分析报告\n")
    A(f"> 数据来源: tushare 年度财务数据 (年报口径) · 分析生成时间 {datetime.now():%Y-%m-%d %H:%M}\n")

    # ---- 3. 财务数据概览 ----
    A("\n## 3. 财务数据概览\n")
    A("| 指标 | " + " | ".join(str(y) for y in years) + " |")
    A("| --- | " + " | ".join(["---"] * n) + " |")
    rows = [
        ("营业收入 (亿)", "total_revenue_亿"),
        ("净利润 (亿)", "net_income_亿"),
        ("毛利率 (%)", "gross_margin_%"),
        ("净利率 (%)", "net_margin_%"),
        ("ROE (%)", "roe_%"),
        ("资产负债率 (%)", "debt_to_assets_%"),
        ("流动比率", "current_ratio"),
        ("总资产周转率 (次)", "assets_turn"),
        ("权益乘数", "equity_multiplier"),
        ("经营现金流 (亿)", "ocf_亿"),
        ("净现比", "净现比"),
        ("货币资金 (亿)", "money_cap_亿"),
        ("应收账款 (亿)", "accounts_receiv_亿"),
        ("存货 (亿)", "inventory_亿"),
        ("合同负债 (亿)", "contract_liab_亿"),
        ("研发费用 (亿)", "rd_exp_亿"),
    ]
    for label, key in rows:
        A(f"| {label} | {col(key)} |")

    # ---- 1. 执行摘要 ----
    A("\n## 1. 执行摘要\n")
    last = financials.get(y1, {})

    def f2(v):
        return "—" if v is None or v != v else f"{v:.2f}"

    A(f"- **营收规模**: {name} 最新报告期营业收入约 **{f2(last.get('total_revenue_亿'))} 亿元**, "
      f"趋势: {_trend(series('total_revenue_亿'))}。")
    A(f"- **盈利能力**: 最新毛利率 **{f2(last.get('gross_margin_%'))}%**、净利率 **{f2(last.get('net_margin_%'))}%**、"
      f"ROE **{f2(last.get('roe_%'))}%**; 毛利率趋势: {_trend(series('gross_margin_%'))}。")
    A(f"- **财务杠杆**: 最新资产负债率 **{f2(last.get('debt_to_assets_%'))}%**; "
      f"流动比率 **{f2(last.get('current_ratio'))}**。")
    A(f"- **现金流质量**: 最新净现比 **{f2(last.get('净现比'))}**"
      f"({'较健康(≥1)' if (last.get('净现比') or 0) >= 1 else '偏低(<1), 需关注利润含金量'})。")
    if valuation:
        A(f"- **当前估值**: 收盘价 {valuation.get('close')} 元, PE(TTM) {valuation.get('pe_ttm')}, "
          f"PB {valuation.get('pb')}, 股息率 {valuation.get('dv_ratio_%')}%。")
    A("\n> 结论: 详见下方逐项分析与综合评估。\n")

    # ---- 2. 公司概况 ----
    A("\n## 2. 公司概况\n")
    A(f"- **公司**: {name}（{code}），所属行业: {info.get('industry', '—')}。")
    A(f"- **分析区间**: {y0}~{y1} 共 {n} 个年度。")
    A("- 本报告基于年度财务报告数据, 采用水平/垂直/比率/杜邦/现金流等分析方法。\n")

    # ---- 4. 逐项深度分析 ----
    A("\n## 4. 逐项深度分析\n")

    # 4.1 资产结构
    A("\n### 4.1 资产结构\n")
    A(f"- 货币资金: {col('money_cap_亿')} 亿元, 趋势 {_trend(series('money_cap_亿'))}。")
    A(f"- 应收账款: {col('accounts_receiv_亿')} 亿元; "
      f"应收账款周转天数: {col('ar_turn', fmt=lambda v: f'{365/v:.0f}天' if v else '—')}。")
    A(f"- 存货: {col('inventory_亿')} 亿元; 存货周转天数: {col('inv_turn', fmt=lambda v: f'{365/v:.0f}天' if v else '—')}。")
    A(f"- 固定资产: {col('fixed_assets_亿')} 亿元。")
    A(f"- 资产结构: 流动资产 {col('total_cur_assets_亿')} 亿元; "
      f"应收/存货若持续快于营收增长, 需警惕流动性质量与减值风险。\n")

    # 4.2 负债结构
    A("\n### 4.2 负债结构\n")
    A(f"- 总负债: {col('total_liab_亿')} 亿元; 资产负债率 {col('debt_to_assets_%')}%, "
      f"趋势 {_trend(series('debt_to_assets_%'), reverse=True)}。")
    A(f"- 流动比率: {col('current_ratio')} (>1 说明短期偿债尚可); 速动比率: {col('quick_ratio')}。")
    A(f"- 合同负债: {col('contract_liab_亿')} 亿元 (预收款/订单储备, 反映未来收入确定性)。\n")

    # 4.3 现金流
    A("\n### 4.3 现金流量\n")
    A(f"- 经营现金流净额: {col('ocf_亿')} 亿元, 趋势 {_trend(series('ocf_亿'))}。")
    A(f"- 投资现金流净额: {col('icf_亿')} 亿元 (负值通常意味着扩张/资本开支)。")
    A(f"- 筹资现金流净额: {col('fcf_亿')} 亿元 (正值偏融资, 负值偏分红/偿债)。")
    A(f"- **净现比** = 经营现金流/净利润: {col('净现比')}。"
      f"最新 {last.get('净现比', '—')}; {'≥1 盈利含金量较好' if (last.get('净现比') or 0) >= 1 else '<1 需关注应收账款/存货占用'}\n")

    # 4.4 盈利能力
    A("\n### 4.4 盈利能力\n")
    A(f"- 营业收入: {col('total_revenue_亿')} 亿元; 同比增速 {col('or_yoy_%', fmt=lambda v: f'{v:+.1f}%')}。")
    A(f"- 净利润: {col('net_income_亿')} 亿元; 同比增速 {col('netprofit_yoy_%', fmt=lambda v: f'{v:+.1f}%')}。")
    A(f"- 毛利率: {col('gross_margin_%')}%, 趋势 {_trend(series('gross_margin_%'))}; "
      f"净利率 {col('net_margin_%')}%。")
    A(f"- 期间费用: 销售 {col('sell_exp_亿')} / 管理 {col('admin_exp_亿')} / 财务 {col('fin_exp_亿')} / "
      f"研发 {col('rd_exp_亿')} 亿元。\n")

    # 4.5 净现比 (已在 4.3 覆盖, 补充说明)
    A("\n### 4.5 净现比专项\n")
    A(f"- 净现比序列: {col('净现比')}。")
    A("- 若净现比持续 <1, 典型原因: 应收账款/存货占用增加、经营性负债减少、或利润含水分; "
      "需结合应收/存货趋势判断现金流质量。\n")

    # 4.6 ROE 杜邦
    A("\n### 4.6 ROE 杜邦分析\n")
    A("ROE = 净利率 × 总资产周转率 × 权益乘数\n")
    A(f"| 年份 | ROE(%) | 净利率(%) | 总资产周转率 | 权益乘数 |")
    A("| --- | --- | --- | --- | --- |")
    for y in years:
        d = financials[y]
        A(f"| {y} | {d.get('roe_%','—')} | {d.get('net_margin_%','—')} | "
          f"{d.get('assets_turn','—')} | {d.get('equity_multiplier','—')} |")
    A("\n- 判断: 高 ROE 来源若主要是高权益乘数(杠杆), 盈利质量弱于高利润率/高周转; "
      "结合净利率与周转率趋势判断 ROE 可持续性。\n")

    # 4.7 分红与估值
    A("\n### 4.7 分红与估值\n")
    if valuation:
        A(f"- 最新估值: 收盘 {valuation.get('close')} 元, PE(TTM) {valuation.get('pe_ttm')}, "
          f"PB {valuation.get('pb')}, 股息率 {valuation.get('dv_ratio_%')}%, 总市值 {valuation.get('total_mv_亿')} 亿元。")
    else:
        A("- 估值数据暂缺。")
    A(f"- 分红能力: 近年净利润 {col('net_income_亿')} 亿元, 经营现金流 {col('ocf_亿')} 亿元, "
      "现金分红可持续性取决于现金流与股利政策。\n")

    # 4.8 风险提示
    A("\n### 4.8 经营/财务风险\n")
    risks = []
    d = last
    if (d.get("净现比") or 0) < 1:
        risks.append(f"净现比 {d.get('净现比')} < 1, 利润含金量不足")
    if (d.get("debt_to_assets_%") or 0) > 60:
        risks.append(f"资产负债率 {d.get('debt_to_assets_%')}% 偏高")
    if (d.get("current_ratio") or 0) < 1:
        risks.append(f"流动比率 {d.get('current_ratio')} < 1, 短期偿债压力")
    if (d.get("gross_margin_%") or 0) < 15:
        risks.append(f"毛利率 {d.get('gross_margin_%')}% 偏低")
    if _trend(series("total_revenue_亿")).startswith("下降"):
        risks.append("营收连续下滑")
    for r in risks:
        A(f"- ⚠️ {r}")
    if not risks:
        A("- 主要量化指标未见明显异常; 具体经营/政策/行业风险需结合财报原文与行业信息。\n")

    # ---- 5. 综合评估 SWOT ----
    A("\n## 5. 综合评估（SWOT）\n")
    A("**优势 (S)**: 数据驱动的盈利能力/现金流/负债结构表现见上; 具体经营优势需结合财报原文判断。")
    A("\n**劣势 (W)**: 上述风险提示中的量化短板; 若净现比<1 或高杠杆则构成主要劣势。")
    A("\n**机会 (O)**: 合同负债(订单)增长、营收加速、毛利率提升等积极信号。")
    A("\n**威胁 (T)**: 行业竞争、政策/监管、原材料与汇率波动等外部因素(需结合行业分析)。\n")

    # ---- 6. 估值判断 ----
    A("\n## 6. 估值判断\n")
    if valuation and valuation.get("pe_ttm"):
        pe = valuation["pe_ttm"]
        if pe < 0:
            verdict = "公司当前亏损或 PE 为负, 建议用 PB/PS 辅助估值"
        elif pe < 15:
            verdict = "PE 处于偏低区间, 估值相对有吸引力"
        elif pe < 30:
            verdict = "PE 处于中等区间, 估值合理"
        else:
            verdict = "PE 偏高, 需高成长支撑, 注意估值回调风险"
        A(f"- 当前 PE(TTM) {pe}、PB {valuation.get('pb')}。定性判断: **{verdict}**。")
        A("- 具体合理估值区间需结合行业可比公司与盈利预测, 建议参考券商一致预期。")
    else:
        A("- 估值数据暂缺, 暂不作区间判断。\n")

    # ---- 7. 风险提示 ----
    A("\n## 7. 风险提示\n")
    for r in (risks or ["以量化指标看暂无突出风险, 但需关注行业与宏观不确定性"]):
        A(f"- {r}")
    A("- 本报告为规则化自动分析, 未覆盖管理层评估/公司治理/未披露诉讼等定性信息; "
      "如需更深入分析, 请在 .env 配置 LLM API Key 后使用 AI 深度分析。\n")

    # ---- 8. 投资建议 ----
    A("\n## 8. 投资建议\n")
    A("- 本报告仅供研究参考, 不构成投资建议。请结合财报原文、行业景气度与个人风险偏好独立决策。")
    A("- 建议持续跟踪: 营收/净利润增速、毛利率、净现比、资产负债率、应收账款与存货周转。\n")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 5) LLM 深度分析 (可选)
# ---------------------------------------------------------------------------

def llm_available() -> bool:
    _load_env()
    return bool(os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY"))


def _call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 5000) -> str:
    """调用 LLM (DeepSeek / OpenAI 兼容接口)。"""
    _load_env()
    key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("未配置 LLM API Key (DEEPSEEK_API_KEY / OPENAI_API_KEY)")
    if os.getenv("DEEPSEEK_API_KEY"):
        base = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        model = os.getenv("LLM_MODEL", "deepseek-chat")
    else:
        base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        },
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _skill_prompt() -> str:
    """读取财报 skill 作为 LLM 系统提示词。"""
    skill_path = PROJECT_ROOT / ".claude" / "skills" / "caibao-skill" / "SKILL.md"
    if skill_path.exists():
        return skill_path.read_text(encoding="utf-8")
    return "你是一位专业财务分析师, 请对给定的公司财报数据进行分析。"


def analyze_llm(info: dict, financials: dict, valuation: dict) -> str:
    """用 LLM 生成深度分析报告 (仅基于 tushare 财务指标, 不依赖 PDF)。"""
    y0 = min(financials.keys()) if financials else ""
    y1 = max(financials.keys()) if financials else ""
    name = info.get("name", "")
    code = info.get("ts_code", "")

    # 指标表
    years = sorted(financials.keys())
    lines = ["年份|" + "|".join(str(y) for y in years)]
    keys = list(financials.get(years[0], {}).keys()) if years else []
    for k in keys:
        vals = "|".join(str(financials[y].get(k)) if financials[y].get(k) is not None else "—" for y in years)
        lines.append(f"{k}|{vals}")
    fin_table = "\n".join(lines)

    user = (
        f"请对上市公司 {name}（{code}）{y0}~{y1} 年度财务报告进行深度分析。\n\n"
        f"## 公司信息\n名称: {name}\n代码: {code}\n行业: {info.get('industry','—')}\n"
        f"最新估值: {valuation if valuation else '暂无'}\n\n"
        f"## tushare 年度财务指标 (tushare 年报口径, 金额单位: 亿元, 比率: %)\n```\n{fin_table}\n```\n\n"
        "请严格按照上述 skill 的输出格式要求 (1 执行摘要 … 8 投资建议) 生成 Markdown 报告, "
        "务必基于给定数据, 严禁编造数字。"
    )
    return _call_llm(_skill_prompt(), user)


# ---------------------------------------------------------------------------
# 5.5) tushare 财务数据入库 / 查询 (pgsql financial_data 表)
# ---------------------------------------------------------------------------

# financials 指标 key (collect_financials 输出) → financial_data 表列名
FIN_PG_MAP = {
    "total_revenue_亿": "total_revenue", "oper_cost_亿": "operate_cost",
    "net_income_亿": "n_income", "gross_margin_%": "gross_margin",
    "net_margin_%": "net_margin", "roe_%": "roe", "or_yoy_%": "or_yoy",
    "netprofit_yoy_%": "netprofit_yoy", "sell_exp_亿": "sell_exp",
    "admin_exp_亿": "admin_exp", "fin_exp_亿": "fin_exp", "rd_exp_亿": "rd_exp",
    "total_assets_亿": "total_assets", "total_cur_assets_亿": "total_cur_assets",
    "money_cap_亿": "money_cap", "accounts_receiv_亿": "accounts_receiv",
    "inventory_亿": "inventory", "fixed_assets_亿": "fixed_assets",
    "contract_liab_亿": "contract_liab", "total_liab_亿": "total_liab",
    "total_cur_liab_亿": "total_cur_liab", "debt_to_assets_%": "debt_to_assets",
    "current_ratio": "current_ratio", "quick_ratio": "quick_ratio",
    "ar_turn": "ar_turn", "inv_turn": "inv_turn",
    "assets_turn": "assets_turn", "equity_multiplier": "equity_multiplier",
    "ocf_亿": "ocf", "icf_亿": "icf", "fcf_亿": "fncf", "净现比": "cash_ratio",
}
FIN_KEY_BY_PG = {v: k for k, v in FIN_PG_MAP.items()}

# 估值快照: financials 估值 dict key → 表列名
_VAL_PG_MAP = {
    "close": "close", "pe_ttm": "pe_ttm", "pb": "pb",
    "dv_ratio_%": "dv_ratio", "total_mv_亿": "total_mv",
}


def financials_to_rows(ts_code: str, name: str, financials: dict,
                       valuation: dict | None = None) -> list[dict]:
    """collect_financials 结果 → financial_data 入库行。"""
    rows = []
    for y, d in financials.items():
        row = {"ts_code": ts_code, "name": name, "year": int(y), "end_date": ""}
        for fin_key, pg_col in FIN_PG_MAP.items():
            v = d.get(fin_key)
            row[pg_col] = None if v is None or v != v else float(v)
        if valuation:
            for vk, pg in _VAL_PG_MAP.items():
                row[pg] = valuation.get(vk)
        rows.append(row)
    return rows


def rows_to_financials(rows: list[dict]) -> dict:
    """financial_data 查询行 → collect_financials 同结构的 {year: 指标}。"""
    financials: dict = {}
    for r in rows:
        y = int(r.get("year") or 0)
        if not y:
            continue
        d = {"year": y}
        for fin_key, pg_col in FIN_PG_MAP.items():
            d[fin_key] = r.get(pg_col)
        financials[y] = d
    return financials


def _is_blank_year(d: dict) -> bool:
    """判断某年财务数据是否完全缺失 (关键指标全为 None, 如 2026 年报未披露)。"""
    if not d:
        return True
    keys = ("total_revenue_亿", "net_income_亿", "total_assets_亿", "roe_%")
    return all(d.get(k) is None for k in keys)


def sync_stock_financial(ts_code: str, years: list[int]) -> int:
    """拉取 tushare 财务数据并入库 financial_data (幂等 upsert), 返回入库行数。"""
    financials = collect_financials(ts_code, years)
    if not financials:
        return 0
    try:
        info = data_service.resolve_code(ts_code)
        name = info.get("name", ts_code.split(".")[0])
    except Exception:
        name = ts_code.split(".")[0]
    valuation = _latest_valuation(ts_code)
    rows = financials_to_rows(ts_code, name, financials, valuation)
    return pg_service.upsert_financial_rows(rows)


def ensure_financials(ts_code: str, years: list[int]) -> tuple[dict, dict]:
    """确保 financial_data 已有该股票年份数据 (缺失自动拉取 tushare 入库)。

    返回 (financials, valuation): financials 为 {year: 指标} 结构, valuation 为最新估值。
    """
    pg_service.init_financial_schema()
    missing = [y for y in years if not pg_service.has_financial(ts_code, y)]
    if missing:
        print(f"  [pgsql] {ts_code} 缺少年份 {missing}, 自动同步 tushare 财务数据入库...")
        sync_stock_financial(ts_code, missing)
    rows = pg_service.query_financial_by_code(ts_code, years)
    financials = rows_to_financials(rows)
    valuation = {}
    if rows:
        last = rows[-1]
        valuation = {vk: last.get(pg) for vk, pg in _VAL_PG_MAP.items()}
    return financials, valuation


# ---------------------------------------------------------------------------
# 6) 主入口
# ---------------------------------------------------------------------------

def analyze(ts_code: str, start_year: int, end_year: int,
            use_llm: bool = False, save_dir: Path | None = None) -> dict:
    """基于 tushare 财报数据 (存 pgsql) 生成分析报告, 不依赖 PDF 下载。

    返回 {info, source, financials, valuation, markdown, llm_used, report_path, range}。
    """
    info = data_service.resolve_code(ts_code)
    ts = info["ts_code"]  # 带后缀, tushare 接口需要 (如 600036.SH)
    if end_year < start_year:
        start_year, end_year = end_year, start_year
    years = list(range(start_year, end_year + 1))

    # 财务数据 (pgsql 优先, 缺失自动同步 tushare 入库)
    financials, valuation = ensure_financials(ts, years)
    # 跳过数据完全缺失的年份 (如 2026 年报未披露 / 旧脏数据全空行)
    financials = {y: d for y, d in financials.items() if not _is_blank_year(d)}
    if not financials:
        raise ValueError(f"未能获取 {info['name']} 的财务数据, 请检查年份范围 (超出已披露范围的部分已跳过)。")

    # 分析
    llm_used = False
    if use_llm and llm_available():
        try:
            markdown = analyze_llm(info, financials, valuation)
            llm_used = True
        except Exception as e:
            print(f"LLM 分析失败, 回退规则化: {e}")
            markdown = analyze_rule_based(info, financials, valuation)
    else:
        markdown = analyze_rule_based(info, financials, valuation)

    # 保存报告
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    symbol = ts.split(".")[0]
    report_path = REPORT_DIR / f"{info.get('name', symbol)}-{symbol}_财报分析_{start_year}_{end_year}.md"
    report_path.write_text(markdown, encoding="utf-8")

    return {
        "info": info,
        "source": "tushare",
        "financials": financials,
        "valuation": valuation,
        "markdown": markdown,
        "llm_used": llm_used,
        "report_path": str(report_path),
        "range": {"start": start_year, "end": end_year},
    }
