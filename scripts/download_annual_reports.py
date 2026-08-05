#!/usr/bin/env python3
"""自动下载 A 股公司最近 N 年（默认 5 年）年度报告 PDF，内置反爬虫策略。

用法:
  python scripts/download_annual_reports.py                          # 全市场最近5年
  python scripts/download_annual_reports.py --codes 600036,000858.SZ # 指定股票
  python scripts/download_annual_reports.py --industry 白酒 --limit 20
  python scripts/download_annual_reports.py --start-year 2021 --end-year 2025
  python scripts/download_annual_reports.py --delay 2.5 --max-retries 4

反爬虫策略:
  - 随机 User-Agent 轮换 (桌面浏览器 UA 池, 每次请求随机取)
  - 每次请求随机延时 (delay 的 60%~140%) + 每 N 次请求长暂停 (5~12s)
  - Referer 伪装 + requests.Session 连接复用 (keep-alive)
  - 指数退避重试 (网络错误 / 非 200 / 非 PDF 头), 上限由 --max-retries 控制
  - 下载后校验 %PDF 文件头, 无效则丢弃重试
  - 支持代理: 设置 HTTP_PROXY / HTTPS_PROXY 环境变量即可
  - 断点续跑: 按年份通配符检查已下载的有效 PDF 并跳过 (校验 %PDF 头与大小, 损坏自动重下)

输出目录: pdf/财报下载/{股票名称}-{代码}/ (可用 --save-dir 修改)
"""

import argparse
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

BASE_URL_TEMPLATE = ("https://money.finance.sina.com.cn/corp/go.php/"
                     "vCB_Bulletin/stockid/{code}/page_type/ndbg.phtml")

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]


def _load_env() -> None:
    """加载项目根目录 .env (TUSHARE_TOKEN 等)。"""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def random_headers(referer: str | None = None) -> dict:
    headers = {
        "User-Agent": random.choice(UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer
    return headers


# ---------------------------------------------------------------------------
# 反爬抓取器
# ---------------------------------------------------------------------------

class Fetcher:
    """带反爬策略的抓取器: 随机UA + 随机延时 + 指数退避重试 + session 复用。"""

    def __init__(self, delay: float = 2.0, max_retries: int = 4,
                 pause_every: int = 30, pause_range: tuple = (5, 12)):
        self.delay = delay
        self.max_retries = max_retries
        self.pause_every = pause_every
        self.pause_range = pause_range
        self.req_count = 0
        self.session = requests.Session()

    def _throttle(self) -> None:
        """请求节流: 每次随机延时, 每 N 次长暂停。"""
        time.sleep(random.uniform(self.delay * 0.6, self.delay * 1.4))
        self.req_count += 1
        if self.req_count % self.pause_every == 0:
            pause = random.uniform(*self.pause_range)
            print(f"    [反爬] 已请求 {self.req_count} 次, 暂停 {pause:.1f}s 避免触发限流...")
            time.sleep(pause)

    def _backoff(self, attempt: int) -> None:
        """指数退避: 1s, 2s, 4s... 上限 20s。"""
        time.sleep(min(2 ** attempt * random.uniform(0.5, 1.5), 20))

    def get(self, url: str, referer: str | None = None, timeout: int = 30):
        """GET 文本页, 失败指数退避重试, 返回 Response 或 None。"""
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, headers=random_headers(referer), timeout=timeout)
                if resp.status_code == 200:
                    return resp
                print(f"    [反爬] HTTP {resp.status_code}, 第 {attempt + 1}/{self.max_retries} 次退避")
            except Exception as e:
                print(f"    [反爬] 请求异常: {e}, 第 {attempt + 1}/{self.max_retries} 次退避")
            self._backoff(attempt)
        return None

    def download(self, url: str, save_path: Path, referer: str | None = None) -> bool:
        """下载文件并校验 %PDF 头, 失败退避重试。"""
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, headers=random_headers(referer),
                                        timeout=90, stream=True)
                if resp.status_code != 200:
                    print(f"    [反爬] HTTP {resp.status_code}, 重试 {attempt + 1}")
                    self._backoff(attempt)
                    continue
                tmp = save_path.with_suffix(".part")
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                if b"%PDF" not in tmp.read_bytes()[:1024]:
                    tmp.unlink(missing_ok=True)
                    print(f"    [反爬] 非 PDF 文件头, 重试 {attempt + 1}")
                    self._backoff(attempt)
                    continue
                tmp.rename(save_path)
                return True
            except Exception as e:
                print(f"    [反爬] 下载异常: {e}, 重试 {attempt + 1}")
                try:
                    save_path.with_suffix(".part").unlink(missing_ok=True)
                except Exception:
                    pass
                self._backoff(attempt)
        return False


# ---------------------------------------------------------------------------
# 新浪年报链接解析 (优先中文版, 英文版仅兜底)
# ---------------------------------------------------------------------------

def _abs_url(href: str) -> str:
    if href.startswith("/"):
        return "https://money.finance.sina.com.cn" + href
    return href if href.startswith("http") else "https://money.finance.sina.com.cn/" + href


def sina_pdf_links(fetcher: Fetcher, code: str, years: list[int]) -> list[tuple]:
    """返回 [(year, detail_url, title)], 优先中文版年报。"""
    url = BASE_URL_TEMPLATE.format(code=code)
    resp = fetcher.get(url)
    if resp is None:
        return []
    resp.encoding = "gb2312"
    soup = BeautifulSoup(resp.text, "html.parser")
    pdf_links: list[tuple] = []

    def match(link):
        href = link.get("href", "")
        title = link.get_text(strip=True)
        if (".pdf" not in href.lower()
                and "年报" not in title and "年度报告" not in title):
            return None
        m = re.search(r"(20\d{2})", title)
        if not m or int(m.group(1)) not in years:
            return None
        return int(m.group(1)), _abs_url(href), title

    for link in soup.find_all("a", href=True):
        hit = match(link)
        if not hit:
            continue
        y, _u, title = hit
        existing = next((i for i, p in enumerate(pdf_links) if p[0] == y), None)
        if existing is None:
            pdf_links.append(hit)
        elif "英文" in pdf_links[existing][2] and "英文" not in title:
            pdf_links[existing] = hit  # 中文版替换英文版

    # 列表页未直接命中 → 遍历公告详情页解析
    if not pdf_links:
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                for col in row.find_all("td"):
                    for a in col.find_all("a", href=True):
                        hit = match(a)
                        if not hit:
                            continue
                        pdf_url = detail_pdf_url(fetcher, hit[1])
                        if pdf_url:
                            pdf_links.append((hit[0], pdf_url, hit[2]))
    return pdf_links


def detail_pdf_url(fetcher: Fetcher, detail_url: str) -> str:
    """从公告详情页解析真实 PDF 下载链接。"""
    resp = fetcher.get(detail_url, referer=BASE_URL_TEMPLATE.split("/corp")[0])
    if resp is None:
        return ""
    resp.encoding = "gb2312"
    soup = BeautifulSoup(resp.text, "html.parser")
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        if ".pdf" in href.lower():
            return href if href.startswith("http") else "https://money.finance.sina.com.cn/" + href
    return ""


# ---------------------------------------------------------------------------
# 股票列表
# ---------------------------------------------------------------------------

def get_stock_list(industry: str = "", limit: int = 0) -> list[tuple]:
    """从 tushare 获取上市 A 股列表, 返回 [(ts_code, name)]。"""
    import tushare as ts
    ts.set_token(os.getenv("TUSHARE_TOKEN", ""))
    pro = ts.pro_api()
    if industry:
        df = pro.stock_basic(industry=industry, list_status="L",
                             fields="ts_code,name,industry")
    else:
        df = pro.stock_basic(list_status="L", fields="ts_code,name,industry")
    if df is None or df.empty:
        return []
    # 仅保留沪深 A 股
    df = df[df["ts_code"].str.endswith((".SH", ".SZ"))]
    if limit and limit > 0:
        df = df.head(limit)
    return [(str(r["ts_code"]), str(r["name"])) for _, r in df.iterrows()]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

MIN_PDF_SIZE = 100 * 1024  # 有效 PDF 最小字节数, 低于此视为损坏/不完整


def find_existing_pdf(stock_dir: Path, symbol: str, year: int) -> Path | None:
    """检查该股票某年份是否已有有效的年报 PDF。

    按 `{symbol}_{year}_*.pdf` 通配符匹配 (新浪标题变化也能识别已下载文件),
    并校验文件头 `%PDF` 与最小大小; 损坏/不完整的旧文件自动删除以便重新下载。
    """
    for f in stock_dir.glob(f"{symbol}_{year}_*.pdf"):
        try:
            if f.stat().st_size >= MIN_PDF_SIZE and b"%PDF" in f.read_bytes()[:1024]:
                return f
            # 损坏 / 不完整: 删除, 让后续重新下载
            f.unlink()
            print(f"    [检查] 检测到损坏文件, 已删除待重下: {f.name}")
        except OSError:
            pass
    return None


def download_one(fetcher: Fetcher, ts_code: str, name: str, years: list[int],
                 save_dir: Path, stats: dict) -> None:
    symbol = ts_code.split(".")[0]
    stock_dir = save_dir / f"{name}-{symbol}"
    stock_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[{stats['done'] + 1}/{stats['total']}] {name} ({ts_code}) 年份 {years[0]}~{years[-1]}")

    pdf_links = sina_pdf_links(fetcher, symbol, years)
    if not pdf_links:
        print(f"  ! 未找到 {name} 的年报链接")
        stats["no_links"] += 1
        return

    for year, detail_url, title in pdf_links:
        safe_title = re.sub(r"[\\/:*?\"<>|]", "_", title)
        save_path = stock_dir / f"{symbol}_{year}_{safe_title}.pdf"
        # 增强: 按年份检查是否已有有效 PDF (标题变化也能识别; 损坏文件被删除后重下)
        existing = find_existing_pdf(stock_dir, symbol, year)
        if existing is not None:
            print(f"  [{year}] 已存在有效年报, 跳过: {existing.name[:44]}")
            stats["skipped"] += 1
            continue
        pdf_url = detail_pdf_url(fetcher, detail_url)
        if not pdf_url:
            print(f"  [{year}] {title} - 未解析到 PDF 链接")
            stats["failed"] += 1
            continue
        print(f"  [{year}] 下载: {title} ...")
        if fetcher.download(pdf_url, save_path, referer=detail_url):
            size_mb = save_path.stat().st_size / 1e6
            print(f"  [{year}] 成功: {save_path.name[:40]}... ({size_mb:.1f}MB)")
            stats["ok"] += 1
        else:
            print(f"  [{year}] 下载失败: {title}")
            stats["failed"] += 1


def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(description="自动下载 A 股公司最近 N 年年度报告 PDF (内置反爬)")
    parser.add_argument("--codes", default="", help="指定股票代码, 逗号分隔 (如 600036,000858.SZ); 空=全市场")
    parser.add_argument("--industry", default="", help="行业名称 (东财分类), 空=全市场")
    parser.add_argument("--limit", type=int, default=0, help="最多处理股票数 (0=不限)")
    parser.add_argument("--start-year", type=int, default=0, help="起始财年, 默认=去年-4")
    parser.add_argument("--end-year", type=int, default=0, help="结束财年, 默认=去年")
    parser.add_argument("--save-dir", default="", help="PDF 保存目录 (默认 pdf/财报下载)")
    parser.add_argument("--delay", type=float, default=2.0, help="每次请求平均延时秒数 (默认 2.0)")
    parser.add_argument("--max-retries", type=int, default=4, help="失败重试次数 (默认 4)")
    parser.add_argument("--pause-every", type=int, default=30, help="每 N 次请求长暂停 (默认 30)")
    args = parser.parse_args()

    # 默认最近 5 个财年: [去年-4 .. 去年]
    cur_year = datetime.now().year
    end_year = args.end_year or (cur_year - 1)
    start_year = args.start_year or (end_year - 4)
    if end_year < start_year:
        start_year, end_year = end_year, start_year
    years = list(range(start_year, end_year + 1))

    save_dir = Path(args.save_dir) if args.save_dir else PROJECT_ROOT / "pdf" / "财报下载"

    # 股票列表
    if args.codes:
        # 补全股票名称 (用于输出目录 {名称}-{代码})
        import tushare as ts
        ts.set_token(os.getenv("TUSHARE_TOKEN", ""))
        pro = ts.pro_api()
        try:
            _df = pro.stock_basic(list_status="L", fields="ts_code,name")
            name_map = {str(r["ts_code"]): str(r["name"]) for _, r in _df.iterrows()}
        except Exception:
            name_map = {}
        stocks = []
        for c in args.codes.split(","):
            c = c.strip()
            if not c:
                continue
            sym = c.split(".")[0]
            ts_code = c if "." in c else f"{sym}.{'SH' if sym.startswith('6') else 'SZ'}"
            stocks.append((ts_code, name_map.get(ts_code, sym)))
    else:
        stocks = get_stock_list(args.industry, args.limit)
    if not stocks:
        print("未获取到股票列表, 请检查 TUSHARE_TOKEN 或 --codes/--industry 参数。")
        return

    if args.limit and args.limit > 0 and not args.codes:
        stocks = stocks[: args.limit]

    print(f"共 {len(stocks)} 只股票, 下载年份 {years}, 保存到 {save_dir}")
    print(f"反爬: 延时={args.delay}s 重试={args.max_retries} 每{args.pause_every}次暂停\n")

    fetcher = Fetcher(delay=args.delay, max_retries=args.max_retries, pause_every=args.pause_every)
    stats = {"done": 0, "total": len(stocks), "ok": 0, "failed": 0, "skipped": 0, "no_links": 0}

    t0 = time.time()
    for ts_code, name in stocks:
        try:
            download_one(fetcher, ts_code, name, years, save_dir, stats)
        except Exception as e:
            print(f"  ! 处理 {name} 异常: {e}")
            stats["failed"] += 1
        stats["done"] += 1

    print(f"\n完成: 成功 {stats['ok']} | 失败 {stats['failed']} | 已存在跳过 {stats['skipped']} "
          f"| 无链接 {stats['no_links']} | 耗时 {(time.time() - t0) / 60:.1f} 分钟")
    print(f"输出目录: {save_dir}")


if __name__ == "__main__":
    main()
