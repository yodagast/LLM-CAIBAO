"""公司大事服务: 网络搜索 (DuckDuckGo/Bing, 免 API Key) + DeepSeek 分批总结 → pgsql stock_events。

设计 (2026-08-14 改造, 解决服务器无法显示/生成不可靠):
  - **持久化任务队列**: 生成任务写入 pg `stock_events_jobs`, 由 `worker_loop` (服务器后台) 或
    脚本 `scripts/sync_stock_events.py` 抢占处理 (FOR UPDATE SKIP LOCKED, 多进程安全)。
    任务存 pg, 服务器多进程/重启不丢失; 崩溃卡死的 processing 任务超时后自动重置重跑。
  - **分批生成 + 进度**: 一次搜索后把 12~20 条大事拆成 _EVENTS_BATCHES 批, 每批调一次 DeepSeek
    (max_tokens 降到 ~6000, 推理更快), 每批完成立即 upsert 入库并更新 job.done_count。
    前端轮询 GET 可看到"已生成 N 条"渐进增长, 而非一直转圈。
  - **错误可见**: 失败写日志 + job.last_error, GET 返回给前端展示, 不再静默重试。

DeepSeek 配置从根目录 .env 读取 (DEEPSEEK_API_KEY / LLM_BASE_URL / LLM_MODEL)。
关键: deepseek-v4-flash 为推理模型, max_tokens 须足够 (每批 6000) 否则 content 为空。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

import httpx

from . import pg_service

logger = logging.getLogger(__name__)

# 分批生成参数: 每批最多 5 条, 共 4 批 ≈ 16~20 条
_EVENTS_BATCH = 5        # 每批最多条数
_EVENTS_BATCHES = 4      # 批数
_EVENTS_TOTAL_EST = 18   # 预估总条数 (进度展示用)

# 全局 httpx 客户端 (异步复用连接)
_client: httpx.AsyncClient | None = None

# 服务器后台 worker 任务引用 (main startup 启动, 防 GC; 多进程各跑一个, 靠 pg 抢占幂等)
worker_task: asyncio.Task | None = None


def _http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
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


async def _summarize_batch(name: str, code: str, snippets: list[dict],
                           batch_no: int, prev_titles: list[str]) -> list[dict]:
    """调用 DeepSeek 生成一批 (≤_EVENTS_BATCH 条) 公司大事 (去重已生成标题)。"""
    from .caibao_service import _call_llm  # 复用 LLM 客户端/配置加载
    sys_prompt = (
        "你是专业的上市公司大事记编辑。用户会给你一家公司的新闻片段(可能来自搜索引擎), "
        "请整理成该公司近 10 年的重大事项时间线(如: 上市/再融资/重大并购/重大合同/业绩里程碑/"
        "高管变动/股权变动/监管事件/重要公告等), 排除广告与无关内容。"
        "严格只输出 JSON 数组, 不要任何其他文字/解释/Markdown 围栏, 格式: "
        '[{"date":"YYYY-MM","title":"一句话标题","summary":"1~2 句摘要"}], '
        f"date 未知时用空字符串 \"\"。本次是第 {batch_no} 批, 只输出 {_EVENTS_BATCH} 条, "
        "必须与下方'已生成标题'不重复, 优先挑选还没覆盖的重要事件。"
    )
    segs = "\n".join(
        f"- [{s.get('date') or '??'}] {s.get('title')}  {s.get('desc') or ''}"
        for s in snippets[:18]
    )
    prev_txt = "\n".join(f"- {t}" for t in prev_titles[:30]) or "(无)"
    user = (
        f"公司: {name} ({code})\n\n"
        f"## 搜索到的新闻片段 (可能为空/不完整)\n{segs or '(无搜索结果, 请基于公开常识生成该公司近10年重大事件)'}\n\n"
        f"## 已生成标题 (勿重复)\n{prev_txt}\n\n"
        f"请输出第 {batch_no} 批 {_EVENTS_BATCH} 条事件的 JSON 数组。"
    )
    try:
        # deepseek-v4-flash 为推理模型, 需足够 max_tokens 让推理完成后再输出内容 (否则 content 为空)
        text = await _call_llm(sys_prompt, user, max_tokens=8000)
    except Exception as e:
        raise RuntimeError(f"DeepSeek 总结公司大事失败: {e}")
    events = _extract_json_array(text)
    if not events:
        raise RuntimeError(f"DeepSeek 第{batch_no}批未返回合法 JSON 数组")
    return events


def is_generating(ts_code: str) -> bool:
    """是否正在生成 (兼容旧调用, 实际由 main 读 job 状态判断)。"""
    return False


async def process_job(ts_code: str, name: str = "") -> dict:
    """处理一个生成任务: 网络搜索 + 分批 DeepSeek + 逐批入库 + 更新 job 进度。

    返回 {"status": "ok"|"empty"|"error", "count": n, "message": str}。
    """
    if not name:
        try:
            from . import data_service
            name = (await data_service.resolve_code(ts_code)).get("name", "")
        except Exception:
            name = ts_code.split(".")[0]
    try:
        await pg_service.update_stock_events_job(
            ts_code, status="processing", total_est=_EVENTS_TOTAL_EST, last_error=None)
        snippets = await _search_web(name, ts_code)
        logger.info("[events] %s %s 搜索到 %d 条片段, 开始分批生成", ts_code, name, len(snippets))
        prev_titles: set[str] = set()
        done = 0
        for batch_no in range(1, _EVENTS_BATCHES + 1):
            events: list[dict] = []
            try:
                events = await _summarize_batch(name, ts_code, snippets,
                                                batch_no, sorted(prev_titles))
            except RuntimeError as e1:
                # 搜索片段异常/无结果时降级: 让 LLM 基于常识生成
                logger.warning("[events] %s 第%d批失败(%s), 降级基于常识重试",
                               ts_code, batch_no, e1)
                try:
                    events = await _summarize_batch(name, ts_code, [], batch_no,
                                                    sorted(prev_titles))
                except RuntimeError as e2:
                    # 单批失败不致命: 跳过该批继续后续 (部分成功也算完成)
                    logger.warning("[events] %s 第%d批降级也失败(%s), 跳过该批",
                                   ts_code, batch_no, e2)
                    continue
            if not events:
                continue
            new = [e for e in events if e["title"] not in prev_titles][:_EVENTS_BATCH]
            if not new:
                continue
            prev_titles.update(e["title"] for e in new)
            n = await pg_service.upsert_stock_events(ts_code, name, new)
            done += n
            await pg_service.update_stock_events_job(ts_code, done_count=done)
            logger.info("[events] %s 第%d批入库 %d 条 (累计 %d)", ts_code, batch_no, n, done)
        if done == 0:
            await pg_service.update_stock_events_job(
                ts_code, status="error", last_error="未生成到有效事件")
            return {"status": "empty", "count": 0, "message": "未生成到有效事件"}
        await pg_service.update_stock_events_job(ts_code, status="done", done_count=done)
        logger.info("[events] %s %s 完成, 共 %d 条", ts_code, name, done)
        return {"status": "ok", "count": done, "message": f"已生成 {done} 条大事"}
    except Exception as e:
        logger.exception("[events] %s 生成失败: %s", ts_code, e)
        try:
            await pg_service.update_stock_events_job(
                ts_code, status="error", last_error=str(e)[:500])
        except Exception:
            pass
        return {"status": "error", "count": 0, "message": str(e)}


async def worker_loop(interval: float = 2.0) -> None:
    """服务器后台任务循环: 抢占 pending job 并处理 (多进程靠 pg 抢占保证幂等)。

    由 main startup 以 asyncio.create_task 启动; 即使某 worker 崩溃, job 仍在 pg,
    其他 worker / 重启后会重新抢占处理。
    """
    logger.info("[events] worker 循环启动")
    while True:
        try:
            job = await pg_service.claim_stock_events_job()
            if job is None:
                await asyncio.sleep(interval)
                continue
            await process_job(job["ts_code"], job.get("name") or "")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[events] worker 循环异常")
            await asyncio.sleep(interval)


async def enqueue_events(ts_code: str, name: str = "", force: bool = False) -> bool:
    """把 ts_code 加入持久化任务队列 (由 worker 处理)。force=True 重置重跑。"""
    return await pg_service.enqueue_stock_events_job(ts_code, name, force=force)


async def enqueue_events_batch(codes: list[str], force: bool = False) -> dict:
    """批量入队 (服务器 POST /batch_sync 用), 返回 {"queued", "total"}。"""
    queued = 0
    for c in codes:
        try:
            if await pg_service.enqueue_stock_events_job(c, "", force=force):
                queued += 1
        except Exception:
            pass
    return {"queued": queued, "total": len(codes)}


async def sync_events(ts_code: str, name: str = "", force: bool = False) -> dict:
    """直接处理单只 (不经过队列; 供脚本/测试调用)。force=True 先清空已有大事。"""
    if force:
        await pg_service.delete_stock_events(ts_code)
    return await process_job(ts_code, name)


async def sync_events_batch(codes: list[str], force: bool = False,
                            limit: int = 0, concurrency: int = 2) -> dict:
    """批量直接处理 (脚本 sync_stock_events.py 用), 跳过已有 (除非 force), 返回汇总。"""
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
