"""SpaceNews 海外抓取脚本（建议在 GitHub Actions 或海外 Linux 节点跑）。

读取 SpaceNews RSS（https://spacenews.com/feed/，WordPress 默认输出包含 content:encoded
全文）。如果你启用了 FiveFilters Full-Text RSS / RSSHub，可以通过 FEED_URL 覆盖默认源。

解析 RSS → 提取每条的全文、图片、发布时间 → POST 到你部署在国内的 weixin_auto_message
服务的 /ingest/spacenews。需要在环境变量里配置：

    INGEST_URL        例：http://your.server.cn:8503/ingest/spacenews
    INGEST_TOKEN      与服务端 SPACENEWS_INGEST_TOKEN 一致
    FEED_URL          可选，默认 https://spacenews.com/feed/
    WINDOW_HOURS      可选，仅推送过去 N 小时内的条目，默认 12

不会重复 POST 已经发过的条目（按 link 比对，写一个 .state.json 在脚本同目录）。
"""
from __future__ import annotations

import json
import os
import random
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
    """构造一组等价但走不同出口 IP 的 RSS 镜像 URL。
    主要思路：让第三方公共服务（RSSHub / Google / Wayback）替我们去抓 spacenews，
    从 spacenews 视角是这些服务的 IP 在请求，绕开 Azure runner IP 段被限流的问题。
    """
    urls = [primary_url]
    # RSSHub 公共实例（来源 https://docs.rsshub.app/guide/instances）
    # /rsshub/spacenews 输出与原 feed 兼容（含 description / pubDate）
    rsshub_path = "/rsshub/spacenews"
    rsshub_hosts = [
        "https://rsshub.app",
        "https://rsshub.rssforever.com",
        "https://rss.shab.fun",
        "https://rsshub.pseudoyu.com",
    ]
    for h in rsshub_hosts:
        urls.append(f"{h}{rsshub_path}")
    # Google web cache（偶尔可用）
    urls.append(f"https://webcache.googleusercontent.com/search?q=cache:{quote(primary_url, safe='')}")
    # Wayback Machine 最近快照（必返回最近一次缓存的 feed，不会是实时的，作最末位兜底）
    urls.append(f"https://web.archive.org/web/2/{primary_url}")
    return urls


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


def main() -> int:
    ingest_url = os.environ.get("INGEST_URL")
    token = os.environ.get("INGEST_TOKEN")
    feed_url = os.environ.get("FEED_URL", "https://spacenews.com/feed/")
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

    print(f"Fetching feed (primary={feed_url})")
    feed_bytes = _fetch_feed(feed_url)
    if feed_bytes is None:
        print("All feed sources failed (likely rate-limited). Exiting cleanly so the "
              "workflow doesn't fail the run.", file=sys.stderr)
        return 0
    parsed = feedparser.parse(feed_bytes)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[dict] = []
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
        # 纯文本备份
        text = " ".join(BeautifulSoup(content_html, "html.parser").get_text(" ").split())
        items.append({
            "title": entry.get("title", "").strip(),
            "link": link,
            "published": dt.isoformat(),
            "summary": text[:500],
            "content_html": content_html,
            "image_url": _first_img(content_html),
            "source": "SpaceNews",
        })

    if not items:
        print("No new items.")
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
