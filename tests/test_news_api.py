import json
from datetime import date

from src import news_api


def _write_cache(root, edition="morning"):
    payload = {
        "date": "2026-08-13",
        "generated_at": "2026-08-13T08:30:00+08:00",
        "spacenews": [{
            "title": "Original title",
            "title_zh": "测试航天新闻",
            "summary_zh": "这是供桌面端显示的中文概要。",
            "body_zh": "第一段全文。\n\n第二段全文。",
            "body_en": "English full text.",
            "source": "SpaceNews",
            "published": "2026-08-13T00:15:00Z",
            "image_url": "https://example.com/hero.jpg",
            "original_link": "https://example.com/article",
            "tags": ["商业航天"],
        }],
        "opml": [],
        "douyin": [],
    }
    (root / f"{edition}_2026-08-13.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_daily_summary_and_full_text_share_stable_item(monkeypatch, tmp_path):
    monkeypatch.setattr(news_api, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(news_api, "PUBLIC_BASE", "https://news.example")
    _write_cache(tmp_path)

    daily = news_api.daily_payload(date(2026, 8, 13), "morning")

    assert daily["count"] == 1
    assert daily["items"][0]["summary"] == "这是供桌面端显示的中文概要。"
    assert daily["items"][0]["image"].startswith(f"{news_api.PUBLIC_BASE}/img?u=")
    assert "w=720" in daily["items"][0]["image"]
    assert daily["items"][0]["page_url"].startswith("https://news.example/news-api/page/")

    detail = news_api.item_payload(daily["items"][0]["id"], date(2026, 8, 13), "morning")
    assert detail["item"]["body_zh"] == "第一段全文。\n\n第二段全文。"
    assert detail["item"]["original_url"] == "https://example.com/article"


def test_daily_page_contains_cards_and_qr(monkeypatch, tmp_path):
    monkeypatch.setattr(news_api, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(news_api, "PUBLIC_BASE", "https://news.example")
    _write_cache(tmp_path)

    page = news_api.render_daily(date(2026, 8, 13), "morning")

    assert "测试航天新闻" in page
    assert "这是供桌面端显示的中文概要" in page
    assert "/news-api/assets/wechat-qr.png" in page
    assert "上午刊" in page and "下午刊" in page


def test_api_docs_are_self_contained(monkeypatch):
    monkeypatch.setattr(news_api, "PUBLIC_BASE", "https://news.example")

    page = news_api.render_api_docs()

    assert "航天速递新闻 API" in page
    assert "/daily?date=" in page
    assert "/openapi.json" in page
    assert "cdn.jsdelivr.net" not in page
    assert "<script" not in page
