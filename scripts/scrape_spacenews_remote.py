"""海外抓取脚本（建议在 GitHub Actions 或海外 Linux 节点跑）。

支持多个 RSS 源，默认抓 SpaceNews + NASASpaceflight。两站在国内 IP 都会被
Cloudflare/BunnyCDN 拒绝，但在 GitHub Actions runner（美国/欧洲出口）一般可以
直接拿到 WordPress 默认输出的 content:encoded 全文。

解析 RSS → 提取每条的全文、图片、发布时间 → POST 到你部署在国内的 weixin_auto_message
服务的 /ingest/spacenews。需要在环境变量里配置：

    INGEST_URL        例：http://your.server.cn:8503/ingest/spacenews
    INGEST_TOKEN      与服务端 SPACENEWS_INGEST_TOKEN 一致
    FEED_URLS         可选，以英文逗号分隔的多个 RSS。每项可写成
                      `<source_name>|<url>`，例如
                          SpaceNews|https://spacenews.com/feed/,NASASpaceflight|https://www.nasaspaceflight.com/feed/
                      不写 source 段时按 host 推断。
    FEED_URL          单源向后兼容（与 FEED_URLS 二选一）。
    WINDOW_HOURS      可选，仅推送过去 N 小时内的条目，默认 12

不会重复 POST 已经发过的条目（按 link 比对，写一个 .state.json 在脚本同目录）。
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests
from bs4 import BeautifulSoup


# 多个 UA 轮换，降低被识别为爬虫的概率
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36 Edg/123.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "feedparser/6.0 +https://github.com/kurtmckee/feedparser",
]


def _fetch_with_retry(url: str, max_tries: int = 4, base_sleep: float = 5.0) -> requests.Response | None:
    """带重试 + UA 轮换 + 指数退避抓单个 URL。429/5xx 会重试，其他错误立即返回。"""
    for attempt in range(1, max_tries + 1):
        ua = random.choice(USER_AGENTS)
        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": ua,
                    "Accept": "application/rss+xml, application/xml, text/xml, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=30,
            )
        except requests.RequestException as e:
            print(f"  [try {attempt}/{max_tries}] {url} -> network error: {e}", file=sys.stderr)
            time.sleep(base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 2))
            continue
        if r.status_code == 200 and r.content:
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            # 优先用服务端给的 Retry-After，否则指数退避
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if (retry_after and retry_after.isdigit()) \
                else base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 3)
            print(f"  [try {attempt}/{max_tries}] {url} -> HTTP {r.status_code}, sleep {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        print(f"  [try {attempt}/{max_tries}] {url} -> HTTP {r.status_code}, giving up", file=sys.stderr)
        return None
    return None


def _build_mirror_urls(primary_url: str) -> list[str]:
    """构造一组等价但走不同出口 IP 的 RSS 镜像 URL。"""
    urls = [primary_url]
    # Wayback Machine 最近快照（最稳定，作为高优先级兜底）
    urls.append(f"https://web.archive.org/web/2/{primary_url}")
    # Google web cache
    urls.append(f"https://webcache.googleusercontent.com/search?q=cache:{quote(primary_url, safe='')}")
    return urls


def _fetch_via_jina(target_url: str) -> str | None:
    """通过 Jina Reader (https://jina.ai/reader/) 代理抓取 URL，返回 markdown 文本。

    Jina 在其服务端发起请求，能稳定绕开 Cloudflare/BunnyCDN 对 GitHub Actions
    runner IP 段的拦截。免费、无需 Key，最大单页 ~120KB。
    """
    proxy = f"https://r.jina.ai/{target_url}"
    for attempt in range(1, 4):
        ua = random.choice(USER_AGENTS)
        try:
            r = requests.get(
                proxy,
                headers={"User-Agent": ua, "Accept": "text/markdown,text/plain,*/*"},
                timeout=45,
            )
        except requests.RequestException as e:
            print(f"  [jina try {attempt}/3] {target_url} network error: {e}", file=sys.stderr)
            time.sleep(3 * attempt)
            continue
        if r.status_code == 200 and r.text:
            return r.text
        print(f"  [jina try {attempt}/3] {target_url} -> HTTP {r.status_code}", file=sys.stderr)
        time.sleep(3 * attempt)
    return None


_LINK_RE = re.compile(r"\(https?://[a-z0-9\-./]*spacenews\.com/[a-z0-9\-/]+\)", re.I)
_NSF_LINK_RE = re.compile(r"\(https?://[a-z0-9\-./]*nasaspaceflight\.com/\d{4}/\d{1,2}/[a-z0-9\-]+/?\)", re.I)


def _scrape_via_jina_homepage(source_name: str, homepage: str) -> list[dict]:
    """RSS 全部失败时，通过 Jina Reader 直接抓首页 → 解析最新文章链接 →
    再用 Jina 抓每篇文章的 markdown，构造与 RSS 路径一致的 item 列表。
    """
    print(f"  Falling back to Jina homepage scrape: {homepage}")
    md = _fetch_via_jina(homepage)
    if not md:
        return []
    # 选 host 对应的链接抽取规则
    host_re = _NSF_LINK_RE if "nasaspaceflight" in homepage else _LINK_RE
    found = []
    seen = set()
    for m in host_re.finditer(md):
        url = m.group(0).strip("()")
        # 跳过 category / tag / author / page 列表页
        path = url.split("//", 1)[-1].split("/", 1)[-1]
        if any(p in path for p in ("/category/", "/tag/", "/author/", "/page/", "#")):
            continue
        if url in seen:
            continue
        seen.add(url)
        found.append(url)
        if len(found) >= 10:
            break
    print(f"  Jina parsed {len(found)} article links from homepage")
    items: list[dict] = []
    now = datetime.now(timezone.utc)
    for url in found:
        body = _fetch_via_jina(url)
        if not body:
            continue
        title = ""
        for line in body.splitlines():
            ls = line.strip()
            if ls.startswith("Title:"):
                title = ls.split(":", 1)[1].strip()
                break
            if ls.startswith("# "):
                title = ls[2:].strip()
                break
        if not title:
            continue
        text = " ".join(body.split())
        items.append({
            "title": title,
            "link": url,
            "published": now.isoformat(),
            "summary": text[:500],
            "content_html": "<div>" + body.replace("\n\n", "</p><p>").replace("\n", " ") + "</div>",
            "image_url": "",
            "source": source_name,
        })
    print(f"  Jina built {len(items)} items via homepage fallback")
    return items


def _fetch_feed(primary_url: str) -> bytes | None:
    """按顺序尝试主源 + 一组镜像，任一成功即返回 feed 字节。"""
    for idx, url in enumerate(_build_mirror_urls(primary_url)):
        print(f"[source {idx + 1}] {url}")
        r = _fetch_with_retry(url, max_tries=3 if idx == 0 else 2, base_sleep=4.0)
        if r is not None and r.content:
            # 简单合法性检查：feedparser 能解析出至少一条条目
            test = feedparser.parse(r.content)
            if test.entries:
                print(f"  ✓ got {len(test.entries)} entries from source {idx + 1}")
                return r.content
            print(f"  ✗ source {idx + 1} returned no entries, trying next")
    return None


def _first_img(html_text: str) -> str:
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    el = soup.find("img")
    if el and el.get("src"):
        return el["src"]
    return ""


def _parse_feed_specs() -> list[tuple[str, str]]:
    """优先用 FEED_URLS（多源），否则回退到 FEED_URL。返回 [(source_name, url)]。"""
    raw = os.environ.get("FEED_URLS", "").strip()
    if not raw:
        single = os.environ.get("FEED_URL", "").strip()
        if single:
            raw = single
        else:
            raw = ("SpaceNews|https://spacenews.com/feed/,"
                   "NASASpaceflight|https://www.nasaspaceflight.com/feed/")
    out: list[tuple[str, str]] = []
    for spec in raw.split(","):
        s = spec.strip()
        if not s:
            continue
        if "|" in s:
            name, url = s.split("|", 1)
            out.append((name.strip(), url.strip()))
        else:
            # 按 host 推断 source 名
            from urllib.parse import urlparse as _up
            host = _up(s).netloc.lower()
            name = "SpaceNews" if "spacenews" in host else \
                   "NASASpaceflight" if "nasaspaceflight" in host else host
            out.append((name, s))
    return out


def main() -> int:
    ingest_url = os.environ.get("INGEST_URL")
    token = os.environ.get("INGEST_TOKEN")
    hours = int(os.environ.get("WINDOW_HOURS", "12"))
    if not ingest_url or not token:
        print("Missing INGEST_URL / INGEST_TOKEN", file=sys.stderr)
        return 2

    state_path = Path(__file__).with_suffix(".state.json")
    seen: set[str] = set()
    if state_path.exists():
        try:
            seen = set(json.loads(state_path.read_text("utf-8")).get("links", []))
        except Exception:
            seen = set()

    feeds = _parse_feed_specs()
    print(f"Will scrape {len(feeds)} feeds: " + ", ".join(f"{n}({u})" for n, u in feeds))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[dict] = []

    for source_name, feed_url in feeds:
        print(f"\n=== Fetching [{source_name}] feed: {feed_url} ===")
        feed_bytes = _fetch_feed(feed_url)
        if feed_bytes is None:
            print(f"  [{source_name}] all feed sources failed; trying Jina homepage fallback",
                  file=sys.stderr)
            # RSS 全军覆没 → 通过 Jina Reader 直接读首页
            homepage = feed_url.replace("/feed/", "/").replace("/feed", "/")
            jina_items = _scrape_via_jina_homepage(source_name, homepage)
            for it in jina_items:
                if it["link"] in seen:
                    continue
                items.append(it)
            continue
        parsed = feedparser.parse(feed_bytes)
        added_for_source = 0
        for entry in parsed.entries:
            link = entry.get("link", "").strip()
            if not link or link in seen:
                continue
            t = entry.get("published_parsed") or entry.get("updated_parsed")
            if not t:
                continue
            dt = datetime(*t[:6], tzinfo=timezone.utc)
            if dt < cutoff:
                continue
            content_html = ""
            if "content" in entry and entry.content:
                content_html = entry.content[0].get("value", "") or ""
            if not content_html:
                content_html = entry.get("summary", "") or ""
            text = " ".join(BeautifulSoup(content_html, "html.parser").get_text(" ").split())
            # 如果 RSS 给的是 excerpt（典型 SpaceNews 只放第一段，<2000 字符）
            # → 用 Jina Reader 拉全文替换
            if len(text) < 2000:
                print(f"  short excerpt ({len(text)} chars) for '{entry.get('title','')[:50]}', "
                      f"fetching full via Jina")
                full_md = _fetch_via_jina(link)
                if full_md and len(full_md) > len(text) + 500:
                    paragraphs = [p.strip() for p in full_md.split("\n\n") if p.strip()]
                    body_html = "".join(
                        f"<p>{p}</p>" for p in paragraphs
                        if not p.startswith(("Title:", "URL Source:", "Markdown Content:", "Image "))
                    )
                    # 保留原 RSS 里的封面 figure（含 og 图）
                    cover_match = re.search(r"<figure[^>]*>.*?</figure>", content_html, re.S | re.I)
                    cover_html = cover_match.group(0) if cover_match else ""
                    content_html = cover_html + body_html
                    text = " ".join(BeautifulSoup(content_html, "html.parser").get_text(" ").split())
                    print(f"  full article via Jina: {len(text)} chars")
            items.append({
                "title": entry.get("title", "").strip(),
                "link": link,
                "published": dt.isoformat(),
                "summary": text[:500],
                "content_html": content_html,
                "image_url": _first_img(content_html),
                "source": source_name,
            })
            added_for_source += 1
        print(f"  [{source_name}] +{added_for_source} new items "
              f"(feed had {len(parsed.entries)} entries)")

    if not items:
        print("No new items across all feeds.")
        return 0

    print(f"POST {len(items)} items -> {ingest_url}")
    resp = requests.post(
        ingest_url,
        headers={"X-Auth-Token": token, "Content-Type": "application/json"},
        data=json.dumps({"articles": items}, ensure_ascii=False).encode("utf-8"),
        timeout=30,
    )
    print(resp.status_code, resp.text[:400])
    resp.raise_for_status()

    seen.update(it["link"] for it in items)
    # 仅保留最近 500 条 link，防文件无限增大
    seen_list = list(seen)[-500:]
    state_path.write_text(json.dumps({"links": seen_list}, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
