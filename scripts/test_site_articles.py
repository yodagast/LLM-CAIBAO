#!/usr/bin/env python3
"""官网文章 (行业研究 / 新闻浏览) 创建、编辑、列表、删除 全链路测试脚本。

用根目录 .env 的 user/passwd 登录后走真实 HTTP API 验证:
  1. 登录 (GET /api/auth/login)
  2. POST /api/site/articles     创建行业研究文章
  3. PUT  /api/site/articles/{id} 编辑该文章
  4. GET  /api/site/articles?kind=research 列表可见
  5. GET  /api/site/articles/{id} 详情可见 (body 正确)
  6. DELETE /api/site/articles/{id} 删除
  7. 断点: 保存后访问 /admin?sitepage=research 应返回后台页面 (200)

用法:
    .venv/bin/python scripts/test_site_articles.py
环境: 需服务运行在 http://127.0.0.1:8000 (BASE_URL 可覆盖)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

PASS = 0
FAIL = 0


def load_env() -> dict:
    """读取根目录 .env (仅 user / passwd / DATABASE_URL)。"""
    env: dict = {}
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


async def main() -> int:
    env = load_env()
    user = env.get("user", "")
    passwd = env.get("passwd", "")
    if not user or not passwd:
        print("❌ .env 缺少 user/passwd")
        return 1

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        print(f"== 登录 ({user}) ==")
        r = await client.post("/api/auth/login",
                              json={"username": user, "password": passwd})
        data = r.json()
        check("登录成功", r.status_code == 200 and data.get("ok"), str(data))
        if r.status_code != 200:
            return 1
        # 保存 cookie (httpx 自动处理 set-cookie)

        print("\n== 创建行业研究文章 (POST /api/site/articles) ==")
        create_payload = {
            "kind": "research",
            "title": "测试-白酒行业研究",
            "date": "2026-08-26",
            "tags": ["行业研究", "自动化测试"],
            "body": "# 白酒行业研究\n\n这是**自动化测试**创建的文章。\n\n- 点一\n- 点二",
        }
        r = await client.post("/api/site/articles", json=create_payload)
        create = r.json()
        check("创建返回 ok", r.status_code == 200 and create.get("ok"), str(create))
        aid = create.get("id")
        check("创建返回 id", aid is not None, str(create))
        if not aid:
            return 1
        print(f"  -> 新文章 id = {aid}")

        print("\n== 列表可见 (GET /api/site/articles?kind=research) ==")
        r = await client.get("/api/site/articles", params={"kind": "research"})
        items = r.json().get("items", [])
        titles = [i.get("title") for i in items]
        check("research 列表含新文章", any(t == create_payload["title"] for t in titles),
              f"{len(items)} 篇, {titles[:5]}")
        # 列表不含 body (减负)
        has_body = any(i.get("body") for i in items)
        check("列表不含 body", not has_body, "列表接口应排除正文")

        print("\n== 详情可见 (GET /api/site/articles/{id}) ==")
        r = await client.get(f"/api/site/articles/{aid}")
        detail = r.json().get("item", {})
        check("详情 title 正确", detail.get("title") == create_payload["title"], str(detail.get("title")))
        check("详情 body 正确", detail.get("body") == create_payload["body"], "")
        check("详情 tags 正确", json.loads(detail.get("tags") or "[]") == create_payload["tags"], str(detail.get("tags")))

        print("\n== 编辑文章 (PUT /api/site/articles/{id}) ==")
        edit_payload = {
            "kind": "research",
            "title": "测试-白酒行业研究(已编辑)",
            "date": "2026-08-25",
            "tags": ["研究"],
            "body": "# 已编辑正文\n\n更新后的内容。",
        }
        r = await client.put(f"/api/site/articles/{aid}", json=edit_payload)
        upd = r.json()
        check("更新返回 ok", r.status_code == 200 and upd.get("ok"), str(upd))
        r = await client.get(f"/api/site/articles/{aid}")
        detail2 = r.json().get("item", {})
        check("编辑后标题生效", detail2.get("title") == edit_payload["title"], str(detail2.get("title")))
        check("编辑后正文生效", detail2.get("body") == edit_payload["body"], "")

        print("\n== 创建新闻浏览文章 + 编辑 (kind=news) ==")
        news_payload = {
            "kind": "news",
            "title": "测试-公司新闻",
            "date": "2026-08-26",
            "tags": ["新闻", "测试"],
            "body": "# 公司新闻\n\n新闻正文。",
        }
        r = await client.post("/api/site/articles", json=news_payload)
        news_id = r.json().get("id")
        check("新闻创建成功", news_id is not None, str(r.json()))
        if news_id:
            r = await client.get("/api/site/articles", params={"kind": "news"})
            news_titles = [i.get("title") for i in r.json().get("items", [])]
            check("news 列表含新文章", any(t == news_payload["title"] for t in news_titles), "")
            # 编辑新闻
            r = await client.put(f"/api/site/articles/{news_id}",
                                 json={**news_payload, "title": "测试-公司新闻(改)"})
            check("新闻编辑成功", r.status_code == 200, str(r.json()))
            r = await client.get(f"/api/site/articles/{news_id}")
            check("新闻编辑后标题生效", r.json().get("item", {}).get("title") == "测试-公司新闻(改)", "")

        print("\n== 前端正文页/列表页可访问 ==")
        for path in ("/research", "/news", f"/article/{aid}"):
            r = await client.get(path)
            check(f"GET {path} -> {r.status_code}", r.status_code == 200)
        # 保存后跳转的后台地址
        r = await client.get("/admin?sitepage=research")
        check("GET /admin?sitepage=research -> 200 (后台可访问)", r.status_code == 200)

        print("\n== 删除测试文章 ==")
        for tid in (aid, news_id):
            if tid:
                r = await client.delete(f"/api/site/articles/{tid}")
                deleted = r.json().get("deleted") if r.status_code == 200 else 0
                check(f"删除文章 {tid}", r.status_code == 200 and deleted == 1, str(r.json()))
        r = await client.get(f"/api/site/articles/{aid}")
        check("删除后详情 404", r.status_code == 404, f"status={r.status_code}")

        print("\n== 鉴权: 未登录创建应 401 ==")
        anon = httpx.AsyncClient(base_url=BASE_URL, timeout=15.0)
        r = await anon.post("/api/site/articles", json=create_payload)
        check("未登录创建 401", r.status_code == 401)
        await anon.aclose()

    print(f"\n======== 测试结果: PASS={PASS}  FAIL={FAIL} ========")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))