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
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup


UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


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

    print(f"Fetching {feed_url}")
    r = requests.get(feed_url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)

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
