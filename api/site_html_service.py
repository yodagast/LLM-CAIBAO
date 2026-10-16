"""导入 HTML 报告的内嵌渲染: 文档拆解 + CSS 作用域隔离。

导入的研究报告是完整独立的 HTML 文档 (自带 <style> 与图表脚本)。为了让它们在
官网里也保留「站点头部 / 页脚」而非整页独占, 这里把文档拆成三部分再内嵌:

  - <style> 全部抽出, 选择器统一加作用域前缀后注入页面 <head>, 隔离报告样式
    (报告里的 :root / body 规则会改写成作用域容器自身, CSS 变量仍可正常继承)
  - <body> 内容注入正文容器
  - <script> 按原顺序在正文之后重新插入 (浏览器解析执行, 不受 innerHTML 限制)

不使用 iframe: 报告表格/图表可直接参与页面滚动与排版。
"""

from __future__ import annotations

import html as _html
import re

# 作用域容器类名 (与 static/article_html.html 中的 .html-doc 一致)
SCOPE = ".html-doc"

_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.S | re.I)
_STYLE_INNER_RE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.S | re.I)
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.S | re.I)
_BODY_RE = re.compile(r"<body\b[^>]*>(.*?)</body\s*>", re.S | re.I)

# 需要递归处理内部规则的 at-rule (其余如 @keyframes / @font-face 原样保留)
_NESTED_AT_RULES = {"media", "supports", "container", "layer", "scope", "document"}

# 报告里代表"文档根"的选择器, 映射为作用域容器自身
_ROOT_SELECTOR_RE = re.compile(r"^(?:html|body|:root)(?![\w-])")


# ---------------------------------------------------------------------------
# 标题 / 日期识别 (后台导入时自动带出)
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_DATE_RE = re.compile(
    r"(?:数据截至|数据截止|报告日期|发布日期|更新日期|发布时间|报告时间|编制日期|日期)"
    r"\s*[:：]?\s*(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")


def extract_title(html_text: str, fallback: str = "") -> str:
    """取 HTML 的 <title> 作为文章标题 (空则回退文件名)。"""
    m = _TITLE_RE.search(html_text)
    if m:
        title = re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()
        if title:
            return title[:200]
    return (fallback or "").strip()[:200]


def extract_date(html_text: str, filename: str = "", fallback: str = "") -> str:
    """识别 HTML 中「数据截至 / 报告日期」等日期, 其次文件名中的 YYYY-MM-DD, 再次 fallback。

    返回值格式 YYYY-MM-DD; 都识别不到时返回空串 (由调用方决定默认值)。
    """
    m = _DATE_RE.search(html_text[:200000])
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m2 = re.search(r"(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})", filename or "")
    if m2:
        return f"{m2.group(1)}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
    return (fallback or "").strip()


# ---------------------------------------------------------------------------
# 文档拆解
# ---------------------------------------------------------------------------

def split_document(html_text: str) -> tuple[str, str, str]:
    """拆解 HTML 文档, 返回 (样式内容, 正文 HTML, 脚本标签 HTML)。

    样式/脚本从正文中移除, 以便分别注入页面 <head> 与正文之后。
    """
    styles = [m.group(1) for m in _STYLE_INNER_RE.finditer(html_text)]
    script_tags = _SCRIPT_RE.findall(html_text)

    body = _BODY_RE.search(html_text)
    content = body.group(1) if body else html_text
    for block in _STYLE_RE.findall(content) + script_tags:
        content = content.replace(block, "")
    return "\n".join(styles), content.strip(), "".join(script_tags)


# ---------------------------------------------------------------------------
# CSS 作用域隔离
# ---------------------------------------------------------------------------

def _next_terminator(css: str, start: int) -> tuple[int, str]:
    """找 start 之后第一个处于顶层的 '{' 或 ';' (跳过注释/字符串/括号内部)。"""
    i, n, depth = start, len(css), 0
    while i < n:
        ch = css[i]
        if ch == "/" and css.startswith("/*", i):
            end = css.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch in "\"'":
            i = _skip_string(css, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in "{;":
            return i, ch
        i += 1
    return -1, ""


def _skip_string(text: str, start: int) -> int:
    """跳过 text[start] 起始的字符串, 返回其结束引号之后的下标。"""
    quote, i, n = text[start], start + 1, len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    return n


def _match_brace(css: str, open_idx: int) -> int:
    """返回与 css[open_idx] ('{') 配对的 '}' 下标 (找不到返回 len(css))。"""
    depth, i, n = 0, open_idx, len(css)
    while i < n:
        ch = css[i]
        if ch == "/" and css.startswith("/*", i):
            end = css.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch in "\"'":
            i = _skip_string(css, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _split_selectors(prelude: str) -> list[str]:
    """按顶层逗号拆分选择器组 (忽略括号 / 字符串 / 注释内的逗号)。"""
    parts: list[str] = []
    buf: list[str] = []
    depth, i, n = 0, 0, len(prelude)
    while i < n:
        ch = prelude[i]
        if ch == "/" and prelude.startswith("/*", i):
            end = prelude.find("*/", i + 2)
            end = n if end == -1 else end + 2
            buf.append(prelude[i:end])
            i = end
            continue
        if ch in "\"'":
            end = _skip_string(prelude, i)
            buf.append(prelude[i:end])
            i = end
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _scope_selector(selector: str, scope: str) -> str:
    """给单个选择器加作用域前缀; html / body / :root 映射为作用域容器自身。"""
    sel = selector.strip()
    if not sel:
        return selector
    rest, hit_root = sel, False
    while True:
        m = _ROOT_SELECTOR_RE.match(rest)
        if not m:
            break
        hit_root = True
        rest = rest[m.end():].lstrip()
        while rest[:1] in (">", "+", "~"):   # html > body 之类的组合符
            rest = rest[1:].lstrip()
    if hit_root:
        return scope + (" " + rest if rest else "")
    return scope + " " + sel


def scope_css(css: str, scope: str = SCOPE) -> str:
    """把整段 CSS 的选择器限制在 scope 容器内 (递归处理 @media 等嵌套 at-rule)。"""
    out: list[str] = []
    pos, n = 0, len(css)
    while pos < n:
        idx, term = _next_terminator(css, pos)
        if idx == -1:
            out.append(css[pos:])
            break
        prelude = css[pos:idx]
        if term == ";":                       # @import / @charset 之类的语句
            out.append(css[pos:idx + 1])
            pos = idx + 1
            continue
        close = _match_brace(css, idx)
        inner = css[idx + 1:close]
        at_name = re.match(r"\s*@([a-zA-Z-]+)", prelude)
        if at_name and at_name.group(1).lower() not in _NESTED_AT_RULES:
            out.append(prelude + "{" + inner + "}")          # @keyframes / @font-face 等
        elif at_name:
            out.append(prelude + "{" + scope_css(inner, scope) + "}")
        else:
            scoped = ", ".join(_scope_selector(s, scope) for s in _split_selectors(prelude))
            out.append(scoped + "{" + inner + "}")
        if close >= n:
            break
        pos = close + 1
    return "".join(out)


# ---------------------------------------------------------------------------
# 对内嵌渲染的统一入口
# ---------------------------------------------------------------------------

def build_embedded(html_text: str, scope: str = SCOPE) -> tuple[str, str, str]:
    """把导入的 HTML 文档转为可内嵌的三部分: (作用域化 CSS, 正文 HTML, 脚本标签)。"""
    css, content, scripts = split_document(html_text)
    return scope_css(css, scope), content, scripts


def has_own_heading(content: str) -> bool:
    """正文是否自带 <h1> (自带则页面不再重复渲染文章标题)。"""
    return bool(re.search(r"<h1\b", content, re.I))
