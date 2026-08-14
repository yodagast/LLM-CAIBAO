"""公司大事服务: 网络搜索 (Bing News RSS, 免 API Key) + DeepSeek 总结 → pgsql stock_events。

流程:
  1. _search_web  : 用 Bing News RSS 搜索 "<公司名> 大事", 取新闻片段 (标题/日期/摘要)
  2. _summarize   : 把片段交给 DeepSeek (LLM) 总结成结构化时间线 JSON [{date, title, summary}]
  3. sync_events  : 解析后 upsert 到 stock_events 表 (按 (ts_code, title) 去重)
  4. GET 接口读库, POST 接口触发 (重新)生成

DeepSeek 配置从根目录 .env 读取 (DEEPSEEK_API_KEY / LLM_BASE_URL / LLM_MODEL),
未配置时仅做网络搜索不入库总结 (或抛错由上层回退)。
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET  # noqa: F401  (保留备用)
from datetime import datetime

import httpx

from . import pg_service

# 正在生成中的 ts_code (防止 GET 自动触发与 POST 手动触发重复/并发)
_INFLIGHT: set[str] = set()

# 全局 httpx 客户端 (异步复用连接)
_client: httpx.AsyncClient | None = None


def _http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    return _client


def _clean_html(text: str) -> str:
    """去 HTML 标签 / 多余空白, 截断过长摘要。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


async def _search_web(name: str, code: str, limit: int = 18) -> list[dict]:
    """免 API Key 网络搜索公司大事, 返回 [{date, title, desc}]。

    依次尝试: DuckDuckGo HTML 端点 → Bing 网页搜索; 都失败返回 [] (上层回退 LLM 知识生成)。
    """
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    query = f"{name} 大事记 公司"
    try:
        items = await _ddg_search(query, limit, headers)
        if items:
            return items
    except Exception:
        pass
    try:
        items = await _bing_search(query, limit, headers)
        if items:
            return items
    except Exception:
        pass
    return []


def _parse_date_from_url(url: str) -> str:
    """从 URL 中尝试提取日期 (YYYYMMDD/YYYY-MM-DD/YYYY/MM/DD), 无则空串。"""
    if not url:
        return ""
    m = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", url)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r"(20\d{2})", url)
    return f"{m.group(1)}-01" if m else ""


async def _ddg_search(query: str, limit: int, headers: dict) -> list[dict]:
    """DuckDuckGo HTML 端点 (免 Key), 解析 result__a 标题 / result__snippet 摘要。"""
    resp = await _http_client().get(
        "https://html.duckduckgo.com/html/", params={"q": query, "kl": "cn-zh"},
        headers=headers)
    resp.raise_for_status()
    html = resp.text
    if "result__a" not in html:
        return []
    items: list[dict] = []
    for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            html, flags=re.S):
        if len(items) >= limit:
            break
        url = m.group(1)
        title = _clean_html(re.sub(r"<[^>]+>", "", m.group(2)))
        desc = _clean_html(re.sub(r"<[^>]+>", "", m.group(3)))
        if not title:
            continue
        items.append({"date": _parse_date_from_url(url), "title": title[:120],
                      "desc": desc[:200]})
    return items


async def _bing_search(query: str, limit: int, headers: dict) -> list[dict]:
    """Bing 网页搜索 (免 Key), 解析 li.b_algo 标题 / <p> 摘要。"""
    resp = await _http_client().get("https://www.bing.com/search",
                                    params={"q": query, "setlang": "zh-CN"},
                                    headers=headers)
    resp.raise_for_status()
    html = resp.text
    if "b_algo" not in html:
        return []
    items: list[dict] = []
    for m in re.finditer(
            r'<li class="b_algo".*?<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>'
            r'(.*?)(?=<li class="b_algo"|</ol>|</div>)',
            html, flags=re.S):
        if len(items) >= limit:
            break
        url = m.group(1)
        title = _clean_html(re.sub(r"<[^>]+>", "", m.group(2)))
        body = _clean_html(re.sub(r"<[^>]+>", " ", m.group(3)))
        if not title:
            continue
        items.append({"date": _parse_date_from_url(url), "title": title[:120],
                      "desc": body[:200]})
    return items


def _extract_json_array(text: str) -> list[dict]:
    """从 LLM 输出中提取 JSON 数组 (容忍 ```json 围栏 / 前后杂文本)。"""
    if not text:
        return []
    s = text.strip()
    # 去 ``` 围栏
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    start, end = s.find("["), s.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for it in data:
        if isinstance(it, dict) and it.get("title"):
            out.append({
                "date": str(it.get("date") or "")[:10],
                "title": str(it["title"])[:255],
                "summary": str(it.get("summary") or "")[:2000],
            })
    return out


async def _summarize(name: str, code: str, snippets: list[dict]) -> list[dict]:
    """调用 DeepSeek 把新闻片段总结成公司大事时间线 JSON。"""
    from .caibao_service import _call_llm  # 复用 LLM 客户端/配置加载
    sys_prompt = (
        "你是专业的上市公司大事记编辑。用户会给你一家公司的新闻片段(可能来自搜索引擎), "
        "请整理成该公司近 10 年的重大事项时间线(如: 上市/再融资/重大并购/重大合同/业绩里程碑/"
        "高管变动/股权变动/监管事件/重要公告等), 排除广告与无关内容。"
        "严格只输出 JSON 数组, 不要任何其他文字/解释/Markdown 围栏, 格式: "
        '[{"date":"YYYY-MM","title":"一句话标题","summary":"1~2 句摘要"}], '
        "date 未知时用空字符串 \"\"。按 date 升序, 取 12~20 条, 标题要具体。"
    )
    segs = "\n".join(
        f"- [{s.get('date') or '??'}] {s.get('title')}  {s.get('desc') or ''}"
        for s in snippets[:18]
    )
    user = (
        f"公司: {name} ({code})\n\n"
        f"## 搜索到的新闻片段 (可能为空/不完整)\n{segs or '(无搜索结果, 请基于公开常识生成该公司近10年重大事件)'}\n\n"
        "请输出上述格式的 JSON 数组。"
    )
    try:
        # deepseek-v4-flash 为推理模型, 需足够 max_tokens 让推理完成后再输出内容 (否则 content 为空)
        text = await _call_llm(sys_prompt, user, max_tokens=16000)
    except Exception as e:
        raise RuntimeError(f"DeepSeek 总结公司大事失败: {e}")
    events = _extract_json_array(text)
    if not events:
        # 容错: LLM 可能输出 Markdown 列表而非 JSON
        raise RuntimeError("DeepSeek 未返回合法 JSON 数组")
    return events


def is_generating(ts_code: str) -> bool:
    return ts_code in _INFLIGHT


async def sync_events(ts_code: str, name: str = "", force: bool = False) -> dict:
    """为单只股票生成/刷新公司大事 (网络搜索 + DeepSeek 总结 + 入库)。

    返回 {"status": "ok"|"empty"|"running"|"error", "count": n, "message": str}。
    """
    if ts_code in _INFLIGHT:
        return {"status": "running", "count": 0, "message": "该股票正在生成中"}
    _INFLIGHT.add(ts_code)
    try:
        if not name:
            try:
                from . import data_service
                name = (await data_service.resolve_code(ts_code)).get("name", "")
            except Exception:
                name = ts_code.split(".")[0]
        if force:
            await pg_service.delete_stock_events(ts_code)
        snippets = await _search_web(name, ts_code)
        try:
            events = await _summarize(name, ts_code, snippets)
        except RuntimeError:
            # 网络搜索无结果时, 让 LLM 基于常识生成 (降级, 标注来源仍为 llm)
            events = await _summarize(name, ts_code, [])
        if not events:
            return {"status": "empty", "count": 0, "message": "未生成到有效事件"}
        n = await pg_service.upsert_stock_events(ts_code, name, events)
        return {"status": "ok", "count": n, "message": f"已生成 {n} 条大事"}
    except Exception as e:
        return {"status": "error", "count": 0, "message": str(e)}
    finally:
        _INFLIGHT.discard(ts_code)


async def sync_events_batch(codes: list[str], force: bool = False,
                            limit: int = 0, concurrency: int = 2) -> dict:
    """批量同步公司大事 (跳过已有, 除非 force), 返回汇总。

    concurrency: DeepSeek 并发上限 (防超频/超时)。limit: 本次最多处理数 (0=不限)。
    """
    if force:
        todo = list(codes)
    else:
        todo = await pg_service.stock_codes_missing_events(codes)
    if limit and limit > 0:
        todo = todo[:limit]
    sem = asyncio.Semaphore(concurrency)
    summary = {"total": len(todo), "ok": 0, "empty": 0, "error": 0, "errors": []}

    async def _one(code: str):
        async with sem:
            r = await sync_events(code, force=force)
            if r["status"] == "ok":
                summary["ok"] += 1
            elif r["status"] == "error":
                summary["error"] += 1
                summary["errors"].append({"code": code, "msg": r["message"]})
            else:
                summary["empty"] += 1

    await asyncio.gather(*(_one(c) for c in todo))
    return summary
