"""航天新闻聚合源：抓取 https://www.spacelive.cn/news。

spacelive.cn 把 NASA / SpaceNews / SpacePolicyOnline 等多个英文航天新闻源在国内做了
聚合，国内服务器可直接访问。我们抓取它的列表页（每条 = 标题 + 来源 + 时间 + 原文链接），
按最近 N 小时过滤；再尝试访问每条「阅读全文」对应的英文站点提取摘要，提取失败的
（多数受 CDN 区域封锁）就只保留 spacelive 给出的标题/来源/时间/链接。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import SETTINGS

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# spacelive 时间字符串：2026年05月22日 09:30:02 （页面默认 Asia/Shanghai）
DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2}):(\d{2})")
CST = timezone(timedelta(hours=8))


@dataclass
class Article:
    title: str
    link: str
    published: str
    summary: str
    source: str
    image_url: str = ""
    content_html: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    retry=retry_if_exception_type((requests.HTTPError, requests.ConnectionError, requests.Timeout)),
    reraise=True,
)
def _http_get(url: str, timeout: int = 15) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def _parse_zh_datetime(s: str) -> datetime | None:
    m = DATE_RE.search(s)
    if not m:
        return None
    y, mo, d, h, mi, se = (int(x) for x in m.groups())
    return datetime(y, mo, d, h, mi, se, tzinfo=CST)


def _extract_summary(html: str, max_len: int = 600) -> str:
    """从外链页面尽量取摘要：og:description -> meta description -> 首段。"""
    soup = BeautifulSoup(html, "lxml")
    for sel in [
        {"property": "og:description"},
        {"name": "description"},
        {"name": "twitter:description"},
    ]:
        m = soup.find("meta", attrs=sel)
        if m and m.get("content"):
            text = " ".join(m["content"].split()).strip()
            if text:
                return text[:max_len] + ("…" if len(text) > max_len else "")
    article = soup.find("article") or soup.find("main") or soup.body
    if article:
        for p in article.find_all("p"):
            text = " ".join(p.get_text(" ").split()).strip()
            if len(text) >= 40:
                return text[:max_len] + ("…" if len(text) > max_len else "")
    return ""


def _fetch_outbound(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if r.status_code >= 400:
            log.info("outbound %s -> HTTP %s (skip)", url, r.status_code)
            return ""
        return _extract_summary(r.text)
    except Exception as e:
        log.info("outbound %s failed: %s (skip)", url, e)
        return ""


def fetch_recent(hours: int = 24, max_items: int = 15, enrich: bool = True) -> list[Article]:
    """抓取近 N 小时国际航天新闻。

    优先使用 data/ingest/ 中由远端 scraper（如 GitHub Actions）POST 进来的全文条目；
    没有时再抓 spacelive.cn 聚合页。"""
    # 1) 优先从 ingest 读
    try:
        from .ingest import load_recent as load_ingest
        recent = load_ingest(hours=hours)
        if recent:
            arts: list[Article] = []
            for x in recent[:max_items]:
                arts.append(Article(
                    title=x.get("title", "").strip(),
                    link=x.get("link", "").strip(),
                    published=x.get("published", "").strip(),
                    summary=x.get("summary", "") or "",
                    source=x.get("source", "SpaceNews"),
                    image_url=x.get("image_url", "") or "",
                    content_html=x.get("content_html", "") or "",
                ))
            log.info("Using ingest data: %d articles", len(arts))
            return arts
    except Exception as e:
        log.warning("ingest load failed, fallback to spacelive: %s", e)

    list_url = "https://www.spacelive.cn/news"
    log.info("Fetching news list: %s", list_url)
    try:
        r = _http_get(list_url)
    except Exception as e:
        log.exception("Fetch spacelive list failed: %s", e)
        return []

    soup = BeautifulSoup(r.text, "lxml")
    cards = soup.select("div.card-body")
    log.info("Parsed %d card blocks", len(cards))

    now = datetime.now(CST)
    cutoff = now - timedelta(hours=hours)
    articles: list[Article] = []

    for c in cards:
        title_el = c.select_one("h3.card-title")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        metas = c.select("p.card-text.text-muted")
        source = metas[0].get_text(strip=True) if len(metas) >= 1 else ""
        pub_raw = metas[1].get_text(strip=True) if len(metas) >= 2 else ""
        dt = _parse_zh_datetime(pub_raw)
        if not dt:
            continue
        if dt < cutoff:
            continue
        link_el = c.find("a", href=True)
        link = link_el["href"] if link_el else ""
        # 同一张 card 的 img.card-img-top（封面图）通常在 card-body 的兄弟节点
        img_url = ""
        parent = c.parent
        if parent:
            img_el = parent.find("img", class_="card-img-top") or parent.find("img")
            if img_el and img_el.get("src"):
                img_url = img_el["src"]
        articles.append(
            Article(
                title=title,
                link=link,
                published=dt.isoformat(),
                summary="",
                source=source or "spacelive",
                image_url=img_url,
            )
        )
        if len(articles) >= max_items:
            break

    # 抓到的条数太少时，自动扩窗口到 24h
    if len(articles) < 3 and hours < 24:
        log.info("Only %d items within %dh, widening window to 24h", len(articles), hours)
        wider_cut = now - timedelta(hours=24)
        seen_links = {a.link for a in articles}
        for c in cards:
            if len(articles) >= max_items:
                break
            title_el = c.select_one("h3.card-title")
            if not title_el:
                continue
            metas = c.select("p.card-text.text-muted")
            source = metas[0].get_text(strip=True) if len(metas) >= 1 else ""
            pub_raw = metas[1].get_text(strip=True) if len(metas) >= 2 else ""
            dt = _parse_zh_datetime(pub_raw)
            if not dt or dt < wider_cut or dt > cutoff:
                continue
            link_el = c.find("a", href=True)
            link = link_el["href"] if link_el else ""
            if link in seen_links:
                continue
            img_url = ""
            parent = c.parent
            if parent:
                img_el = parent.find("img", class_="card-img-top") or parent.find("img")
                if img_el and img_el.get("src"):
                    img_url = img_el["src"]
            articles.append(
                Article(
                    title=title_el.get_text(strip=True),
                    link=link,
                    published=dt.isoformat(),
                    summary="",
                    source=source or "spacelive",
                    image_url=img_url,
                )
            )
            seen_links.add(link)

    if not articles:
        log.info("No items within %dh, falling back to top 5 latest", hours)
        for c in cards[:5]:
            title_el = c.select_one("h3.card-title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            metas = c.select("p.card-text.text-muted")
            source = metas[0].get_text(strip=True) if len(metas) >= 1 else ""
            pub_raw = metas[1].get_text(strip=True) if len(metas) >= 2 else ""
            dt = _parse_zh_datetime(pub_raw) or now
            link_el = c.find("a", href=True)
            link = link_el["href"] if link_el else ""
            img_url = ""
            parent = c.parent
            if parent:
                img_el = parent.find("img", class_="card-img-top") or parent.find("img")
                if img_el and img_el.get("src"):
                    img_url = img_el["src"]
            articles.append(
                Article(title=title, link=link, published=dt.isoformat(), summary="", source=source or "spacelive", image_url=img_url)
            )

    if enrich:
        for a in articles:
            if a.link:
                a.summary = _fetch_outbound(a.link)

    log.info("spacelive returned %d articles (with %d having summary)", len(articles), sum(1 for a in articles if a.summary))
    return articles
