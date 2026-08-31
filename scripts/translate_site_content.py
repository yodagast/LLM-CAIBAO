"""将现有中文官网内容翻译为英文并写入 *_en 字段 (site_pages.content_en / site_articles.title_en+body_en)。

幂等: 只填充英文为空的行; 已有英文则跳过。
用法: uv run python scripts/translate_site_content.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import pg_service  # noqa: E402

# ---------- site_pages 英文内容 (key -> content_en JSON/文本) ----------
SITE_PAGES_EN = {
    "vision": json.dumps({
        "title": "Vision",
        "lede": "Compounding of time and knowledge.",
        "body": ("Respect Knowledge | Compounding goes beyond capital; it comes from the "
                 "cognition, decisions and mistakes accumulated over the long term.\n"
                 "Respect Time | Embrace the slowness and solitude along the way, and build "
                 "moats through time.\n"
                 "Fiduciary Duty | Create value impartially for clients, partners, and the "
                 "companies we invest in.\n"
                 "Compounding Life | Achieve your own compounding life through aligned "
                 "knowledge and action."),
    }, ensure_ascii=False),
    "mission": json.dumps({
        "title": "Mission",
        "lede": "Serve every client impartially and create reasonable, sustainable returns above the social average.",
        "body": ("Reasonable | Only seek profits within our circle of competence.\n"
                 "Sustainable | Grow profits through the compounding of time and knowledge.\n"
                 "Beyond Mediocrity | Aim for returns above the average over the long term, "
                 "even if losses are possible in the short run or in bull markets.\n"
                 "Beyond Profit | We regard solving real social needs and exploring science "
                 "and true knowledge as our original aspiration and mission."),
    }, ensure_ascii=False),
    "values": json.dumps([
        {"name": "Objectivity", "desc": "Free ourselves from emotions and bias; work and live grounded in facts."},
        {"name": "Rationality", "desc": "Base decisions on facts and logic, and master multi-disciplinary thinking models."},
        {"name": "Responsibility", "desc": "Do the right things and do things right; adhere to our stop-doing list."},
        {"name": "Honesty", "desc": "Do not hide ignorance or exaggerate ability; say what we know and admit what we don't."},
        {"name": "Continuous Learning", "desc": "Make learning a lifelong habit and keep evolving amid uncertainty."},
        {"name": "Unity of Knowledge and Action", "desc": "Avoid losses rather than chase profits; avoid foolishness rather than seek cleverness."},
    ], ensure_ascii=False),
}

# ---------- site_articles 英文标题/正文 (article_id -> (title_en, body_en)) ----------
ARTICLES_EN = {
    16: ("Shandong Pharmaceutical Glass",
         "Shandong Pharmaceutical Glass — a leading Chinese manufacturer of pharmaceutical "
         "packaging, specialized in borosilicate glass tubing and injectable vials."),
    1: ("Baijiu Industry Deep Dive",
         "Premiumization and inventory cycles…"),
    6: ("CaiBao Capital Donated to Xinhua Education Foundation",
         "In February 2026, CaiBao Capital donated to the Xinhua Education Foundation to "
         "support the training of young financial talent."),
    5: ("CaiBao Capital Hosted an Invitation-Only Investor Salon on Long-Termism & Compounding",
         "*April 2026*\nTag: Investor Relations"),
    4: ("CaiBao Capital Spoke at the Asia Value Investing Forum",
         "*June 2026*\nTag: Industry Exchange"),
    3: ("CaiBao Capital Published Its Annual Value Investing Outlook",
         "*July 2026*\nTag: Research Publication"),
    2: ("CaiBao Capital Launched Its Smart Stock Screening & Backtesting Platform",
         "*August 2026*\nTag: Company News"),
}


async def main() -> int:
    updated_pages = 0
    updated_articles = 0

    # 1. site_pages: 填充 content_en
    pages = await pg_service.get_site_pages()
    for key, en_content in SITE_PAGES_EN.items():
        page = pages.get(key)
        if not page:
            continue
        if page.get("content_en"):
            print(f"[skip] site_pages.{key} 已有英文, 跳过")
            continue
        await pg_service.upsert_site_page(
            key, page.get("title") or "", page.get("content") or "", en_content)
        updated_pages += 1
        print(f"[ok] site_pages.{key} 已写入英文")

    # 2. site_articles: 填充 title_en / body_en
    for kind in ("research", "news"):
        rows = await pg_service.list_site_articles(kind)
        for r in rows:
            aid = r["id"]
            pair = ARTICLES_EN.get(aid)
            if not pair:
                print(f"[warn] 未找到文章 {aid} 的英文翻译, 跳过")
                continue
            full = await pg_service.get_site_article(aid)
            if full.get("title_en") or full.get("body_en"):
                print(f"[skip] 文章 {aid} 已有英文, 跳过")
                continue
            title_en, body_en = pair
            await pg_service.update_site_article(
                aid, full["kind"], full["title"], full["date"], full["tags"],
                full["body"], title_en, body_en)
            updated_articles += 1
            print(f"[ok] 文章 {aid} ({full['title']}) 已写入英文")

    print(f"\n完成: site_pages 更新 {updated_pages}, site_articles 更新 {updated_articles}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))