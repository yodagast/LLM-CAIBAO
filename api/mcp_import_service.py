"""MCP 接口导入 HTML 服务: 自动校验 + 入暂存区 (待人工校验)。

流程:
  1. MCP 客户端 (任意 LLM/Agent) 用 `X-MCP-Token` 调用 POST /api/mcp/articles/import
  2. 服务自动校验 HTML (结构完整性 / 标题 / 日期 / 正文体量 / 危险内容 / 重复)
  3. 校验通过与否都先落入 `site_article_drafts` 暂存区 (status=pending), 不直接发布
  4. 管理员在后台「内容管理 → 待校验」面板人工复核并修正标题/日期/标签
  5. 人工确认「通过」后才转正为 site_articles 文章; 「驳回」则记录原因归档

安全: 自动校验只是给人工复核提供参考 (warnings), 不做拦截 —— 最终发布权在人。
"""

from __future__ import annotations

import html as _html
import json
import re
from pathlib import Path

from . import pg_service, site_html_service

# 与 main.py 保持一致 (导入默认标签 / 正文上限)
DEFAULT_IMPORT_TAGS = ["AI研究"]
MAX_ARTICLE_BODY = 8_000_000


def _plain_text(raw: str) -> str:
    """HTML → 纯文本 (去 style/script/标签/实体), 用于空壳内容与体量判断。"""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", str(raw or ""))
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()

# 危险内容检测 (导入的报告不应含这些, 命中则提示人工重点复核)
_DANGEROUS_RE = [
    (re.compile(r"<script[^>]+\bsrc\s*=\s*[\"']?(?!https?://)", re.I), "存在本地相对路径脚本引用"),
    (re.compile(r"\beval\s*\(", re.I), "正文含 eval() 调用"),
    (re.compile(r"\bdocument\.cookie\b", re.I), "正文含 document.cookie 访问"),
    (re.compile(r"<iframe\b", re.I), "正文含 iframe 嵌套"),
    (re.compile(r"javascript\s*:", re.I), "正文含 javascript: 伪协议链接"),
]
# 常见"空壳"提示语 (报告未正常生成的特征)
_EMPTY_HINTS = ["暂无数据", "无数据", "no data", "生成失败", "报告生成中", "placeholder"]


def validate_html(raw: str, filename: str = "", title: str = "",
                  date: str = "", kind: str = "") -> dict:
    """自动校验导入的 HTML, 返回 {passed, errors[], warnings[], fields{}}。

    errors 表示硬性问题 (仍可入库待人工判断); warnings 表示需人工重点确认。
    fields 为自动识别出的字段 (标题/日期/标签/统计), 供后台校验表单预填。
    """
    text = str(raw or "")
    errors: list[str] = []
    warnings: list[str] = []

    # --- 1) 结构完整性 ---
    if not text.strip():
        errors.append("HTML 内容为空")
    head = text.lstrip()[:200].lower()
    if not head.startswith(("<!doctype", "<html")):
        errors.append("不是完整 HTML 文档 (缺少 <!doctype html> / <html> 根标签)")
    if "<body" not in text.lower():
        warnings.append("未找到 <body> 标签 (可能无法正确内嵌渲染)")
    if "</html>" not in text.lower():
        warnings.append("未找到 </html> 结束标签 (文件可能被截断)")

    # --- 2) 字段识别 ---
    auto_title = site_html_service.extract_title(text, Path(filename).stem)
    auto_date = site_html_service.extract_date(text, filename)
    final_title = (title or "").strip() or auto_title
    final_date = (date or "").strip() or auto_date
    if not final_title:
        errors.append("无法识别标题 (HTML 无 <title> 且未指定 title)")
    if not final_date:
        warnings.append("未识别到报告日期 (建议人工填写)")
    if not auto_title and title.strip():
        warnings.append("HTML 无 <title>, 使用了调用方传入的标题")

    # --- 3) 正文体量 / 图表脚本 ---
    body_len = len(text)
    if body_len < 500:
        warnings.append(f"正文体量偏小 ({body_len} 字符), 请确认报告内容完整")
    if body_len > MAX_ARTICLE_BODY:
        errors.append(f"正文过大 ({body_len} 字符 > 上限 {MAX_ARTICLE_BODY})")
    has_script = bool(re.search(r"<script\b", text, re.I))
    has_chart = bool(re.search(r"echarts|chart\.js|highcharts|plotly|d3\.", text, re.I))
    if has_chart and not has_script:
        warnings.append("检测到图表库关键字但无 <script> 标签, 图表可能无法渲染")

    # --- 4) 空壳内容检测 (去标签后判断) ---
    plain_clean = _plain_text(text)
    if len(plain_clean) < 50:
        errors.append("去除标签后正文几乎为空")
    hit_empty = [h for h in _EMPTY_HINTS if h in plain_clean[:2000]]
    if hit_empty and len(plain_clean) < 400:
        warnings.append(f"正文疑似空壳内容 (命中「{hit_empty[0]}」)")

    # --- 5) 危险内容 ---
    for pat, msg in _DANGEROUS_RE:
        if pat.search(text):
            warnings.append(msg)

    # --- 6) 标题/日期合理性 ---
    if final_title and len(final_title) > 120:
        warnings.append(f"标题过长 ({len(final_title)} 字), 建议精简")
    if final_title and re.match(r"^(untitled|新建|document|index)", final_title, re.I):
        warnings.append(f"标题疑似占位符「{final_title}」, 建议人工改写")
    if final_date and not re.match(r"^\d{4}", final_date):
        warnings.append(f"日期格式非 YYYY 开头「{final_date}」, 请确认")

    passed = not errors
    return {
        "passed": passed,
        "errors": errors,
        "warnings": warnings,
        "fields": {
            "title": final_title,
            "date": final_date,
            "auto_title": auto_title,
            "auto_date": auto_date,
            "kind": kind or "research",
            "body_len": body_len,
            "has_script": has_script,
            "has_chart": has_chart,
            "plain_len": len(plain_clean),
        },
    }


async def _duplicate_of(raw: str, title: str) -> dict | None:
    """检测重复: 已发布文章标题相同, 或已有待校验草稿标题相同。"""
    if not title:
        return None
    try:
        items, _ = await pg_service.list_site_articles("research", 1, 500, title, "", "newest")
        for it in items:
            if str(it.get("title") or "").strip() == title.strip():
                return {"where": "article", "id": it.get("id"), "title": it.get("title")}
    except Exception:
        pass
    try:
        drafts, _ = await pg_service.list_site_article_drafts("pending", "", 1, 200)
        for d in drafts:
            if str(d.get("title") or "").strip() == title.strip():
                return {"where": "draft", "id": d.get("id"), "title": d.get("title")}
    except Exception:
        pass
    return None


async def import_html(raw: str, kind: str = "research", filename: str = "",
                      title: str = "", date: str = "",
                      tags: list[str] | None = None, source: str = "mcp") -> dict:
    """MCP 导入入口: 自动校验 → 写入待校验暂存区。返回 {ok, draft_id, checks, ...}。"""
    kind = kind if kind in ("research", "news") else "research"
    filename = Path(str(filename or "")).name[:255]
    checks = validate_html(raw, filename, title, date, kind)

    dup = await _duplicate_of(raw, checks["fields"]["title"])
    if dup:
        checks["warnings"].append(
            f"标题与已有{'文章' if dup['where'] == 'article' else '待校验草稿'}"
            f"「{dup['title']}」(id={dup['id']}) 重复")
        checks["duplicate"] = dup

    final_tags = [str(x).strip() for x in (tags or []) if str(x).strip()] \
        or list(DEFAULT_IMPORT_TAGS)

    draft_id = await pg_service.create_site_article_draft(
        kind=kind,
        title=checks["fields"]["title"],
        date=checks["fields"]["date"],
        tags=json.dumps(final_tags, ensure_ascii=False),
        body=str(raw or ""),
        src_file=filename,
        source=source,
        checks=json.dumps(checks, ensure_ascii=False))

    return {
        "ok": True,
        "draft_id": draft_id,
        "status": "pending",
        "need_review": True,
        "message": "已写入待校验暂存区, 请在后台「内容管理 → 待校验」人工复核后发布",
        "auto_checks": checks,
        "title": checks["fields"]["title"],
        "date": checks["fields"]["date"],
        "tags": final_tags,
        "kind": kind,
        "src_file": filename,
        "body_len": len(str(raw or "")),
    }


async def publish_draft(did: int, title: str = "", date: str = "",
                        tags: list[str] | None = None, kind: str = "",
                        note: str = "") -> dict:
    """人工校验通过: 用（可能已修正的）字段转正为正式文章。"""
    draft = await pg_service.get_site_article_draft(did)
    if not draft:
        return {"ok": False, "message": f"草稿 {did} 不存在"}
    if str(draft.get("status")) != "pending":
        return {"ok": False, "message": f"草稿 {did} 状态为 {draft.get('status')}, 不可重复校验"}

    final_kind = kind if kind in ("research", "news") else str(draft.get("kind") or "research")
    final_title = (title or "").strip() or str(draft.get("title") or "").strip()
    if not final_title:
        return {"ok": False, "message": "标题为空, 无法发布 (请先补充标题)"}
    final_date = (date or "").strip() or str(draft.get("date") or "").strip()
    if tags is None:
        try:
            final_tags = json.loads(str(draft.get("tags") or "[]"))
            final_tags = [str(x) for x in final_tags] if isinstance(final_tags, list) else []
        except (TypeError, ValueError):
            final_tags = []
    else:
        final_tags = [str(x).strip() for x in tags if str(x).strip()]
    if not final_tags:
        final_tags = list(DEFAULT_IMPORT_TAGS)

    aid = await pg_service.create_site_article(
        final_kind, final_title, final_date,
        json.dumps(final_tags, ensure_ascii=False),
        str(draft.get("body") or ""),
        format="html", src_file=str(draft.get("src_file") or ""))

    await pg_service.mark_site_article_draft(did, "published", note, aid)
    return {"ok": True, "draft_id": did, "article_id": aid, "title": final_title,
            "date": final_date, "tags": final_tags, "kind": final_kind,
            "url": f"/article/{aid}"}


async def reject_draft(did: int, note: str = "") -> dict:
    """人工校验驳回: 记录原因归档 (不发布)。"""
    draft = await pg_service.get_site_article_draft(did, with_body=False)
    if not draft:
        return {"ok": False, "message": f"草稿 {did} 不存在"}
    if str(draft.get("status")) != "pending":
        return {"ok": False, "message": f"草稿 {did} 状态为 {draft.get('status')}, 不可重复校验"}
    n = await pg_service.mark_site_article_draft(did, "rejected", note)
    return {"ok": bool(n), "draft_id": did, "status": "rejected", "note": note}