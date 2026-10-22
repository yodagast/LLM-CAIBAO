# MCP 内容导入接口文档

QICIFIER 官网内容管理提供 **MCP (Model Context Protocol) 风格**的 HTTP 接口，
供外部 Agent / LLM 工具把生成的 HTML 研究报告推送到本站。

## 核心约定：先校验，后发布

```
MCP 客户端                服务端                      管理员
    │                       │                          │
    │  POST /import ───────►│                          │
    │                       │ 自动校验 (结构/标题/日期/  │
    │                       │ 体量/危险内容/重复)        │
    │◄── draft_id (pending)─┤ 写入待校验暂存区           │
    │                       │                          │
    │  GET /drafts ────────►│                          │
    │◄── status: pending ───┤                          │
    │                       │◄── 后台人工复核 ──────────┤
    │                       │    (修正标题/日期/标签)    │
    │                       │◄── 通过 / 驳回 ──────────┤
    │                       │ 通过 → 发布为正式文章      │
    │  GET /drafts ────────►│                          │
    │◄── status: published ─┤  (含 article_id)          │
```

> **导入不会直接对外发布。** 所有 MCP 导入的 HTML 都会先进入「待校验」队列，
> 由管理员在后台「内容管理 → 待校验」面板人工复核确认后才转正为正式文章。
> 这样即使 Agent 生成的内容有问题，也不会直接出现在官网上。

---

## 1. 鉴权

所有 MCP 接口都需要在请求头中携带 token：

| 方式 | 请求头 |
|---|---|
| 推荐 | `X-MCP-Token: <MCP_TOKEN>` |
| 兼容 | `Authorization: Bearer <MCP_TOKEN>` |

`MCP_TOKEN` 配置在项目根目录 `.env`：

```env
# MCP (Model Context Protocol) 接口鉴权 token
MCP_TOKEN=b6C4yiAmCdCVj9KRYrSasKCv2FIvogSEmagIAjXPOTM
```

管理员也可在后台「内容管理 → **MCP 导入**」按钮弹窗中查看 / 复制该 token。

| 状态码 | 含义 |
|---|---|
| `401` | 缺少 MCP 凭证 (未带 token) |
| `403` | MCP 凭证无效 |
| `503` | 服务端未配置 `MCP_TOKEN` |

---

## 2. 接口列表

基础地址：`http://127.0.0.1:8000`（生产环境替换为实际域名）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/mcp/health` | 连通性与鉴权自检 |
| `POST` | `/api/mcp/articles/import` | 导入 HTML（自动校验 + 入待校验队列） |
| `POST` | `/api/mcp/articles/validate` | 仅做自动校验，**不入库** |
| `GET` | `/api/mcp/articles/drafts` | 查询草稿及其实时校验状态 |

---

### 2.1 `GET /api/mcp/health`

连通性与鉴权自检，返回待校验数量。

```bash
curl -H "X-MCP-Token: $MCP_TOKEN" http://127.0.0.1:8000/api/mcp/health
```

```json
{
  "ok": true,
  "service": "qicifier-content-mcp",
  "version": "1.0",
  "pending_reviews": 0
}
```

---

### 2.2 `POST /api/mcp/articles/import`

导入 HTML 报告正文，自动校验后写入待校验暂存区。

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `html` | string | ✅ | 完整 HTML 文档全文（上限 8 MB） |
| `kind` | string | | `research` 行业研究（默认）/ `news` 公司新闻 |
| `filename` | string | | 原始文件名，用于自动识别标题/日期 |
| `title` | string | | 标题（留空则自动取 `<title>` 或文件名） |
| `date` | string | | 展示日期（留空则自动识别「数据截至 / 报告日期」等） |
| `tags` | string[] | | 标签列表（默认 `["AI研究"]`） |

**响应**（`200`）

```json
{
  "ok": true,
  "draft_id": 1,
  "status": "pending",
  "need_review": true,
  "message": "已写入待校验暂存区, 请在后台「内容管理 → 待校验」人工复核后发布",
  "title": "贵州茅台600519-SH深度研究报告",
  "date": "2026-03-28",
  "tags": ["AI研究"],
  "kind": "research",
  "src_file": "贵州茅台600519-SH深度研究报告.html",
  "body_len": 335,
  "auto_checks": {
    "passed": true,
    "errors": [],
    "warnings": ["正文体量偏小 (335 字符), 请确认报告内容完整"],
    "fields": {
      "title": "贵州茅台600519-SH深度研究报告",
      "date": "2026-03-28",
      "kind": "research",
      "body_len": 335,
      "has_script": true,
      "has_chart": false,
      "plain_len": 127
    }
  }
}
```

**调用示例**

```bash
curl -X POST "http://127.0.0.1:8000/api/mcp/articles/import" \
  -H "Content-Type: application/json" \
  -H "X-MCP-Token: $MCP_TOKEN" \
  -d @- <<'JSON'
{
  "kind": "research",
  "filename": "宁德时代300750-SZ深度研究报告.html",
  "html": "<!DOCTYPE html><html><head><title>宁德时代深度研究</title></head><body>…</body></html>"
}
JSON
```

Python 客户端：

```python
import json
import os
import urllib.request

MCP_TOKEN = os.getenv("MCP_TOKEN")
BASE = "http://127.0.0.1:8000"

def mcp_import(html: str, filename: str, kind: str = "research") -> dict:
    body = json.dumps({"kind": kind, "html": html, "filename": filename}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/mcp/articles/import", data=body,
        headers={"Content-Type": "application/json", "X-MCP-Token": MCP_TOKEN})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())

with open("报告.html", encoding="utf-8") as f:
    print(mcp_import(f.read(), "报告.html"))
```

---

### 2.3 `POST /api/mcp/articles/validate`

**只校验不入库**。Agent 可在正式导入前自查 HTML 质量，提前发现缺标题、体量过小等问题。

请求体与 `import` 相同；响应：

```json
{
  "ok": true,
  "auto_checks": {
    "passed": false,
    "errors": ["无法识别标题 (HTML 无 <title> 且未指定 title)"],
    "warnings": ["未识别到报告日期 (建议人工填写)"],
    "fields": { "...": "..." }
  }
}
```

---

### 2.4 `GET /api/mcp/articles/drafts`

查询草稿队列，用于跟踪导入是否已被人工作业发布。

| 参数 | 默认 | 说明 |
|---|---|---|
| `status` | `pending` | `pending` 待校验 / `published` 已通过 / `rejected` 已驳回 / `all` |
| `page` | `1` | 页码 |
| `page_size` | `20` | 每页数量（1~200） |

```bash
curl -H "X-MCP-Token: $MCP_TOKEN" \
  "http://127.0.0.1:8000/api/mcp/articles/drafts?status=published&page=1&page_size=20"
```

```json
{
  "status": "published",
  "page": 1,
  "page_size": 20,
  "total": 1,
  "count": 1,
  "items": [
    {
      "id": 1,
      "kind": "research",
      "title": "贵州茅台600519-SH深度研究报告",
      "date": "2026-03-28",
      "tags": "[\"AI研究\"]",
      "src_file": "贵州茅台600519-SH深度研究报告.html",
      "status": "published",
      "source": "mcp",
      "article_id": 25,
      "created_at": "2026-10-22T16:25:01",
      "reviewed_at": "2026-10-22T16:31:20",
      "review_note": ""
    }
  ]
}
```

---

## 3. 自动校验规则（供人工复核参考）

自动校验只提供**提示**，不做拦截 —— 最终是否发布由管理员人工判断。

### 3.1 错误（errors）— 硬性问题

| 规则 | 说明 |
|---|---|
| HTML 内容为空 | `html` 为空串 |
| 缺少根标签 | 开头不是 `<!doctype html>` / `<html>` |
| 无法识别标题 | HTML 无 `<title>` 且未传 `title` |
| 正文过大 | 超过 8 MB 上限 |
| 正文几乎为空 | 去除标签后纯文本 < 50 字符 |

### 3.2 提示（warnings）— 需人工重点确认

| 规则 | 说明 |
|---|---|
| 未找到 `<body>` / `</html>` | 文档结构不完整或被截断 |
| 未识别到报告日期 | 建议人工填写 |
| 正文体量偏小 | < 500 字符 |
| 图表库关键字但无 `<script>` | 图表可能无法渲染 |
| 正文疑似空壳内容 | 命中「暂无数据 / 生成失败」等且内容过短 |
| 存在本地相对路径脚本 / `eval()` / `document.cookie` / `<iframe>` / `javascript:` | 安全风险 |
| 标题过长 / 疑似占位符 | 如 `Untitled`、`新建文档`、`index` |
| 日期格式非 `YYYY` 开头 | 格式异常 |
| 标题与已有文章/草稿重复 | 提示可能重复导入 |

### 3.3 自动识别字段

| 字段 | 识别方式 |
|---|---|
| 标题 | `<title>` 内容 → 文件名（去扩展名） |
| 日期 | 正文中的「数据截至 / 数据截止 / 报告日期 / 发布日期 / 更新日期 / 发布时间 / 报告时间 / 编制日期 / 日期」后的日期 → 文件名中的 `YYYY-MM-DD` |
| 标签 | 调用方传入 → 默认 `["AI研究"]` |

---

## 4. 后台人工校验流程

1. 用管理员账号（`yodagast`）登录 `/admin`
2. 进入 **页面管理 → 行业研究 / 公司新闻**
3. 点击 **「待校验」** 按钮（按钮上的红色数字表示待处理条数）
4. 左侧队列点选草稿，右侧显示：
   - **自动校验结果**（错误 / 提示 / 来源 / 字符数 / 是否含图表脚本）
   - **可编辑字段**：类型、日期、标题、标签、校验备注
   - **预览原文**：沙箱 iframe 渲染 HTML 原文（隔离自带脚本，避免影响后台页面）
5. 三个操作：
   - **通过并发布** —— 先保存修正后的字段，再转正为正式文章（可点「新窗口打开」查看文章页）
   - **仅保存修改** —— 只更新草稿字段，状态仍为待校验
   - **驳回** —— 记录原因并归档，不发布

> 通过 / 驳回 **不可重复执行**：草稿一旦离开 `pending` 状态即视为已完结
> （接口会返回 `400`），已发布文章后续修改请走文章编辑页。

---

## 5. 状态流转

```
                    ┌──────────┐
   MCP 导入 ───────►│ pending  │ 待校验
                    └────┬─────┘
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
        ┌───────────┐         ┌──────────┐
        │ published │ 已通过   │ rejected │ 已驳回
        └───────────┘         └──────────┘
        生成 site_articles     归档, 记 review_note
        (article_id 回填)
```

---

## 6. 前端页面地址

| 地址 | 说明 |
|---|---|
| `/research` | 行业研究列表（含内容搜索 + 分页） |
| `/news` | 公司新闻列表 |
| `/article/{id}` | 文章正文页（HTML 报告内嵌站点头尾渲染） |

---

## 7. 常见问题

**Q: 导入后为什么在官网看不到？**
A: 按设计如此。MCP 导入先进入待校验队列，需管理员在后台「待校验」面板确认发布。

**Q: 一次能导入多大文件？**
A: 上限 8 MB。研究报告通常 100 KB ~ 2 MB，正常范围内。

**Q: 报告自带的 ECharts 图表会正常显示吗？**
A: 会。正文渲染时会抽取报告的 `<style>`（加作用域前缀隔离）与 `<script>`（按原顺序重放），
图表脚本可正常执行，同时报告的样式不会污染官网其它页面。

**Q: token 泄露怎么办？**
A: 修改根目录 `.env` 中的 `MCP_TOKEN`（服务端每次请求实时读文件，**无需重启**即生效）。

**Q: 如何只校验不导入？**
A: 用 `POST /api/mcp/articles/validate`。

**Q: Agent 如何知道内容已发布？**
A: 轮询 `GET /api/mcp/articles/drafts?status=published`，命中后取 `article_id`
拼出文章链接 `/article/{article_id}`。