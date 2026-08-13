"""Public, read-only daily space-news API and web pages for desktop clients.

The API reads the already generated morning/evening cache files.  It never
invokes an LLM and never starts a scraper from a request handler.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache"
QR_FILE = ROOT / "data" / "assets" / "wechat-qr.png"
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "https://links.he-ting.com").rstrip("/")
EDITIONS = ("morning", "evening")
MAX_HISTORY_DAYS = 31
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_value(raw: str | None) -> date:
    if not raw or raw == "today":
        return date.today()
    if not _DATE_RE.fullmatch(raw):
        raise HTTPException(status_code=400, detail="date 格式应为 YYYY-MM-DD")
    try:
        value = date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="无效日期") from exc
    today = date.today()
    if value > today or value < today - timedelta(days=MAX_HISTORY_DAYS - 1):
        raise HTTPException(status_code=400, detail=f"仅支持今天及此前 {MAX_HISTORY_DAYS - 1} 天")
    return value


def _edition_value(raw: str) -> str:
    if raw not in EDITIONS:
        raise HTTPException(status_code=400, detail="edition 仅支持 morning 或 evening")
    return raw


def _cache_path(day: date, edition: str) -> Path:
    return CACHE_DIR / f"{edition}_{day.isoformat()}.json"


def _load_cache(day: date, edition: str) -> dict:
    path = _cache_path(day, edition)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="当日新闻缓存暂不可用") from exc
    return payload if isinstance(payload, dict) else {}


def _text(value: object) -> str:
    return str(value or "").strip()


def _plain(value: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _text(value))).strip()


def _short(value: object, limit: int = 220) -> str:
    text = _plain(value)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _item_id(kind: str, article: dict) -> str:
    raw = "|".join((kind, _text(article.get("original_link") or article.get("link")), _text(article.get("title_zh") or article.get("title"))))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _absolute_image(value: str) -> str:
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"{PUBLIC_BASE}/{value.lstrip('/')}"


def _iter_articles(payload: dict):
    for key, kind in (("spacenews", "intl"), ("opml", "gzh"), ("douyin", "douyin")):
        for article in payload.get(key) or []:
            if isinstance(article, dict):
                yield kind, article


def _normalize(kind: str, article: dict, day: date, edition: str) -> dict:
    item_id = _item_id(kind, article)
    title = _text(article.get("title_zh") or article.get("title") or article.get("desc") or "未命名新闻")
    summary = _short(article.get("summary_zh") or article.get("description") or article.get("summary") or article.get("body_zh") or article.get("desc"))
    image = _absolute_image(_text(article.get("image_url") or article.get("image") or article.get("cover")))
    original = _text(article.get("original_link") or article.get("share_url") or article.get("link"))
    return {
        "id": item_id,
        "kind": kind,
        "title": title,
        "summary": summary,
        "image": image,
        "source": _text(article.get("source") or ("公众号" if kind == "gzh" else "航天速递")),
        "published": _text(article.get("published")),
        "tags": article.get("tags") or [],
        "page_url": f"{PUBLIC_BASE}/news-api/page/{day.isoformat()}/{edition}/{item_id}",
        "original_url": original,
    }


def _edition_meta(day: date, edition: str) -> dict:
    payload = _load_cache(day, edition)
    return {
        "available": bool(payload),
        "count": sum(1 for _ in _iter_articles(payload)),
        "generated_at": _text(payload.get("generated_at")),
    }


def daily_payload(day: date, edition: str) -> dict:
    payload = _load_cache(day, edition)
    items = [_normalize(kind, article, day, edition) for kind, article in _iter_articles(payload)]
    return {
        "ok": True,
        "date": day.isoformat(),
        "edition": edition,
        "title": f"{day.isoformat()} 航天速递 · {'上午刊' if edition == 'morning' else '下午刊'}",
        "generated_at": _text(payload.get("generated_at")),
        "count": len(items),
        "items": items,
        "editions": {name: _edition_meta(day, name) for name in EDITIONS},
        "web_url": f"{PUBLIC_BASE}/news-api/page/{day.isoformat()}/{edition}",
        "qr_url": f"{PUBLIC_BASE}/news-api/assets/wechat-qr.png",
    }


def _find_item(item_id: str, requested_day: date | None = None, requested_edition: str | None = None) -> tuple[date, str, str, dict]:
    today = date.today()
    days = [requested_day] if requested_day else [today - timedelta(days=offset) for offset in range(MAX_HISTORY_DAYS)]
    editions = [requested_edition] if requested_edition else list(EDITIONS)
    for day in days:
        for edition in editions:
            for kind, article in _iter_articles(_load_cache(day, edition)):
                if _item_id(kind, article) == item_id:
                    return day, edition, kind, article
    raise HTTPException(status_code=404, detail="未找到该新闻条目")


def item_payload(item_id: str, requested_day: date | None = None, requested_edition: str | None = None) -> dict:
    day, edition, kind, article = _find_item(item_id, requested_day, requested_edition)
    item = _normalize(kind, article, day, edition)
    item.update({
        "body_zh": _text(article.get("body_zh") or article.get("description") or article.get("summary") or article.get("desc")),
        "body_en": _text(article.get("body_en") or article.get("summary_en")),
        "summary_zh": _text(article.get("summary_zh") or article.get("description") or article.get("summary")),
        "title_orig": _text(article.get("title") or article.get("title_en")),
        "content_complete": bool(article.get("body_zh") or article.get("body_en")),
    })
    return {"ok": True, "date": day.isoformat(), "edition": edition, "item": item}


def _paragraphs(value: str) -> str:
    pieces = [piece.strip() for piece in re.split(r"\n\s*\n|\r?\n", value or "") if piece.strip()]
    return "".join(f"<p>{html.escape(piece)}</p>" for piece in pieces)


_STYLE = """
*{box-sizing:border-box}body{margin:0;background:#071018;color:#dbe7ed;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;line-height:1.7}
main{max-width:980px;margin:auto;padding:34px 20px 64px}a{color:#64d4d0;text-decoration:none}.top{display:flex;gap:12px;justify-content:space-between;align-items:end;margin-bottom:24px}.top h1{margin:0;font-size:26px}.muted{color:#78909c;font-size:13px}
.editions{display:flex;gap:8px}.editions a{border:1px solid #28414c;border-radius:8px;padding:7px 13px}.editions a.active{background:#17383c;border-color:#52bdb9;color:#dff}.cards{display:grid;gap:14px}.card{display:grid;grid-template-columns:220px 1fr;gap:18px;padding:14px;border:1px solid #21333e;border-radius:12px;background:#0d1821}.card img{width:220px;height:130px;object-fit:cover;border-radius:8px;background:#14232c}.card h2{font-size:18px;margin:0 0 7px}.card p{margin:0 0 10px;color:#9fb0b9}.meta{font-size:12px;color:#667e89}.links{display:flex;gap:14px;margin-top:8px}.empty{padding:60px;text-align:center;border:1px dashed #2c424c;border-radius:12px;color:#78909c}.article{max-width:780px;margin:auto}.article h1{font-size:28px;line-height:1.35}.hero{width:100%;max-height:440px;object-fit:cover;border-radius:12px;margin:18px 0}.summary{background:#10232b;border-left:3px solid #56c9c4;padding:14px 18px;border-radius:8px}.body p{text-indent:2em;margin:0 0 18px}.qr{margin:42px auto 0;padding-top:28px;border-top:1px solid #20323c;text-align:center}.qr img{width:230px;height:230px;border-radius:12px;background:#fff}.qr p{color:#8ca0aa;font-size:13px}
@media(max-width:680px){.top{display:block}.editions{margin-top:14px}.card{grid-template-columns:1fr}.card img{width:100%;height:190px}}
"""


def _qr_footer() -> str:
    return f'<footer class="qr"><img src="{PUBLIC_BASE}/news-api/assets/wechat-qr.png" alt="航天速递二维码"><p>扫码关注航天速递，获取每日更新</p></footer>'


def render_daily(day: date, edition: str) -> str:
    payload = daily_payload(day, edition)
    cards = []
    for item in payload["items"]:
        image = f'<img src="{html.escape(item["image"])}" alt="" loading="lazy">' if item["image"] else '<div></div>'
        original = f'<a href="{html.escape(item["original_url"])}" target="_blank" rel="noopener">原始网址</a>' if item["original_url"] else ""
        cards.append(
            f'<article class="card">{image}<div><div class="meta">{html.escape(item["source"])} · {html.escape(item["published"])}</div>'
            f'<h2><a href="{html.escape(item["page_url"])}">{html.escape(item["title"])}</a></h2><p>{html.escape(item["summary"])}</p>'
            f'<div class="links"><a href="{html.escape(item["page_url"])}">阅读全文</a>{original}</div></div></article>'
        )
    content = '<div class="cards">' + "".join(cards) + "</div>" if cards else '<div class="empty">该时段暂无新闻</div>'
    tabs = "".join(
        f'<a class="{"active" if name == edition else ""}" href="{PUBLIC_BASE}/news-api/page/{day.isoformat()}/{name}">{"上午刊" if name == "morning" else "下午刊"}</a>'
        for name in EDITIONS
    )
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(payload["title"])}</title><style>{_STYLE}</style></head><body><main><header class="top"><div><div class="muted">SPACE NEWS DAILY</div><h1>{html.escape(payload["title"])}</h1><div class="muted">共 {payload["count"]} 条 · 仅展示该时段内容</div></div><nav class="editions">{tabs}</nav></header>{content}{_qr_footer()}</main></body></html>'


def render_item(item_id: str, day: date, edition: str) -> str:
    item = item_payload(item_id, day, edition)["item"]
    hero = f'<img class="hero" src="{html.escape(item["image"])}" alt="">' if item["image"] else ""
    original = f'<p><a href="{html.escape(item["original_url"])}" target="_blank" rel="noopener">查看原始网址</a></p>' if item["original_url"] else ""
    body = _paragraphs(item["body_zh"] or item["summary"])
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(item["title"])}</title><style>{_STYLE}</style></head><body><main class="article"><a href="{PUBLIC_BASE}/news-api/page/{day.isoformat()}/{edition}">← 返回当日新闻</a><h1>{html.escape(item["title"])}</h1><div class="meta">{html.escape(item["source"])} · {html.escape(item["published"])}</div>{hero}<section class="summary">{html.escape(item["summary_zh"] or item["summary"])}</section><div class="body">{body}</div>{original}{_qr_footer()}</main></body></html>'


news_app = FastAPI(
    title="航天速递新闻 API",
    version="1.0.0",
    description="公开只读 API：获取近 31 天早晚航天新闻概要与全文；请求过程不调用 GPT。",
)


@news_app.get("/", include_in_schema=False)
def news_home():
    edition = "morning" if datetime.now().hour < 16 else "evening"
    return RedirectResponse(f"/news-api/page/{date.today().isoformat()}/{edition}")


@news_app.get("/dates", summary="列出可查看的新闻日期")
def news_dates():
    cutoff = date.today() - timedelta(days=MAX_HISTORY_DAYS - 1)
    found: dict[str, list[str]] = {}
    for path in CACHE_DIR.glob("*_????-??-??.json"):
        match = re.fullmatch(r"(morning|evening)_(\d{4}-\d{2}-\d{2})\.json", path.name)
        if not match:
            continue
        try:
            value = date.fromisoformat(match.group(2))
        except ValueError:
            continue
        if cutoff <= value <= date.today():
            found.setdefault(value.isoformat(), []).append(match.group(1))
    return {"ok": True, "days": [{"date": key, "editions": sorted(value)} for key, value in sorted(found.items(), reverse=True)]}


@news_app.get("/daily", summary="获取某日某时段新闻概要")
def news_daily(date_: str | None = Query(default=None, alias="date"), edition: str = "morning"):
    return daily_payload(_date_value(date_), _edition_value(edition))


@news_app.get("/item/{item_id}", summary="获取新闻全文与原文信息")
def news_item(item_id: str, date_: str | None = Query(default=None, alias="date"), edition: str | None = None):
    day = _date_value(date_) if date_ else None
    selected = _edition_value(edition) if edition else None
    return item_payload(item_id, day, selected)


@news_app.get("/page/{date_}/{edition}", response_class=HTMLResponse, include_in_schema=False)
def daily_page(date_: str, edition: str):
    return HTMLResponse(render_daily(_date_value(date_), _edition_value(edition)))


@news_app.get("/page/{date_}/{edition}/{item_id}", response_class=HTMLResponse, include_in_schema=False)
def item_page(date_: str, edition: str, item_id: str):
    day = _date_value(date_)
    return HTMLResponse(render_item(item_id, day, _edition_value(edition)))


@news_app.get("/assets/wechat-qr.png", include_in_schema=False)
def qr_asset():
    if not QR_FILE.exists():
        raise HTTPException(status_code=404, detail="二维码未配置")
    return FileResponse(QR_FILE, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
