"""NASA / ESA 官网 RSS 抓取，产出与 SpaceNews 兼容的英文新闻条目（dict）。

本服务器实测可直连 nasa.gov / esa.int 的 RSS，因此直接抓取，再交由
news_pages 抓全文 + 翻译（与 SpaceNews 同一套流程）。每个条目字段对齐 SpaceNews：
    title / link / published(ISO) / summary / source / image_url / content_html
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

from .config import SETTINGS

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


def _feeds() -> dict[str, str]:
    return {"NASA": SETTINGS.nasa_rss, "ESA": SETTINGS.esa_rss}


def _img_from_summary(html: str) -> str:
    if not html:
        return ""
    m = _IMG_RE.search(html)
    return m.group(1).strip() if m else ""


def _clean(html: str, max_len: int = 500) -> str:
    if not html:
        return ""
    text = " ".join(BeautifulSoup(html, "lxml").get_text(" ").split())
    return text[:max_len].rstrip() + ("…" if len(text) > max_len else "")


def fetch_source(source: str, hours: int, max_items: int = 8) -> list[dict]:
    """抓取单个来源（NASA / ESA）近 N 小时的条目。窗口内不足 2 条时放宽到 48h。"""
    url = _feeds().get(source, "")
    if not url:
        return []
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    except Exception as e:
        log.warning("%s feed fetch failed: %s", source, e)
        return []

    def _collect(win_hours: int) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=win_hours)
        items: list[dict] = []
        for e in parsed.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            if not t:
                continue
            dt = datetime(*t[:6], tzinfo=timezone.utc)
            if dt < cutoff:
                continue
            link = (e.get("link") or "").strip()
            if not link:
                continue
            summ_html = e.get("summary", "") or e.get("description", "")
            items.append({
                "title": (e.get("title") or "").strip(),
                "link": link,
                "published": dt.isoformat(),
                "summary": _clean(summ_html),
                "source": source,
                "image_url": _img_from_summary(summ_html),
                "content_html": "",
            })
            if len(items) >= max_items:
                break
        return items

    out = _collect(hours)
    if len(out) < 2 and hours < 48:
        wider = _collect(48)
        if len(wider) > len(out):
            log.info("%s: only %d within %dh, widened to 48h -> %d", source, len(out), hours, len(wider))
            out = wider
    log.info("%s feed: %d items", source, len(out))
    return out


def fetch_all(hours: int, max_per_source: int = 8) -> list[dict]:
    """抓取 NASA + ESA 全部条目。"""
    out: list[dict] = []
    for s in ("NASA", "ESA"):
        try:
            out += fetch_source(s, hours, max_per_source)
        except Exception as e:
            log.warning("%s fetch failed: %s", s, e)
    return out
