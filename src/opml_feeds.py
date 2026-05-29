"""读取 OPML，抓取其中各订阅源近一天的条目。"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

from .config import SETTINGS

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


@dataclass
class OpmlEntry:
    source: str
    title: str
    link: str
    published: str
    description: str
    image_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def parse_opml(path) -> list[dict]:
    """解析 OPML，返回 [{title, xmlUrl, htmlUrl}]"""
    root = ET.parse(path).getroot()
    feeds = []
    for outline in root.iter("outline"):
        url = outline.get("xmlUrl")
        if not url:
            continue
        feeds.append({
            "title": outline.get("text") or outline.get("title") or url,
            "xmlUrl": url,
            "htmlUrl": outline.get("htmlUrl"),
        })
    return feeds


def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def _clean_html(html: str, max_len: int = 500) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    text = " ".join(soup.get_text(" ").split())
    return text[:max_len].rstrip() + ("…" if len(text) > max_len else "")


_MP_OG_DESC = re.compile(r'<meta[^>]+(?:property|name)=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']', re.I)
_MP_DESC_VAR = re.compile(r'(?:var\s+msg_desc|msg_desc)\s*=\s*["\']([^"\']+)["\']')
_MP_OG_IMAGE = re.compile(r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']*)["\']', re.I)
_MP_COVER_VAR = re.compile(r'(?:var\s+msg_cdn_url|msg_cdn_url)\s*=\s*["\']([^"\']+)["\']')


def _fetch_mp_meta(url: str) -> tuple[str, str]:
    """对微信公众号文章页面尝试取出 (description, image_url)。"""
    if "mp.weixin.qq.com" not in url:
        return "", ""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            return "", ""
        html = r.text
        desc = ""
        m = _MP_OG_DESC.search(html) or _MP_DESC_VAR.search(html)
        if m:
            desc = _clean_html(m.group(1))
        else:
            soup = BeautifulSoup(html, "lxml")
            content = soup.find(id="js_content") or soup.find("div", class_="rich_media_content")
            if content:
                desc = _clean_html(str(content), 400)
        img = ""
        mi = _MP_OG_IMAGE.search(html) or _MP_COVER_VAR.search(html)
        if mi:
            img = mi.group(1).strip()
        return desc, img
    except Exception as e:
        log.warning("fetch mp meta failed %s: %s", url, e)
    return "", ""


def fetch_opml_recent(hours: int = 48, max_per_feed: int = 10, fetch_desc: bool = True) -> list[OpmlEntry]:
    feeds = parse_opml(SETTINGS.opml_path)
    log.info("Parsed %d feeds from OPML", len(feeds))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    results: list[OpmlEntry] = []

    for f in feeds:
        url = f["xmlUrl"]
        title = f["title"]
        log.info("Fetching feed: %s (%s)", title, url)
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
        except Exception as e:
            log.warning("fetch feed %s failed: %s", url, e)
            continue

        kept = 0
        for entry in parsed.entries:
            if kept >= max_per_feed:
                break
            dt = _entry_datetime(entry)
            if not dt or dt < cutoff:
                continue
            link = entry.get("link", "").strip()
            desc = _clean_html(entry.get("summary", "") or entry.get("description", ""))
            img = ""
            if fetch_desc and link:
                mp_desc, mp_img = _fetch_mp_meta(link)
                if not desc:
                    desc = mp_desc
                img = mp_img
            results.append(
                OpmlEntry(
                    source=title,
                    title=entry.get("title", "").strip(),
                    link=link,
                    published=dt.isoformat(),
                    description=desc,
                    image_url=img,
                )
            )
            kept += 1
        log.info("  -> %d recent entries", kept)

    log.info("OPML total recent entries: %d", len(results))
    return results
