"""专题情报：海外抓取脚本（在 GitHub Actions 海外 runner 上运行）。

流程：
1. 从服务器公开接口拉取专题条目列表（url / id / region）。
2. 用 Jina Reader (https://r.jina.ai/<url>) 抓取每条全文（可绕开 Cloudflare/限流，
   也能读 PDF），抽取正文段落与文中图片。
3. POST {articles:[{url,id,title,text,images}]} 到服务器 /ingest/topic。

环境变量（沿用 SpaceNews 那套，无需新增 secret）：
    INGEST_URL    例 http://<server>:<port>/ingest/spacenews
                  （脚本会据此推出根地址、/ingest/topic 与 /api/topic/get）
    INGEST_TOKEN  与服务器 .env 的 SPACENEWS_INGEST_TOKEN 一致
    TOPIC_ID      可选，默认 space-tug
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time

import requests

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36 Edg/123.0",
]

_JINA_BODY_RE = re.compile(r"Markdown Content:\s*\n", re.I)
_IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://[^)]+\)")
_NAV_KEYWORDS_RE = re.compile(
    r"(Subscribe|Sign\s*In|Search\s+for|^Menu$|Posted in|TOPICS:|Tagged:|Filed Under:|"
    r"Newsletters?|Cookie|Privacy Policy|Terms of Use|Skip to (content|main))",
    re.I,
)


def _fetch_via_jina(target_url: str) -> str | None:
    proxy = f"https://r.jina.ai/{target_url}"
    for attempt in range(1, 4):
        try:
            r = requests.get(
                proxy,
                headers={"User-Agent": random.choice(USER_AGENTS),
                         "Accept": "text/markdown,text/plain,*/*",
                         "X-Return-Format": "markdown"},
                timeout=60,
            )
        except requests.RequestException as e:
            print(f"  [jina {attempt}/3] {target_url} network error: {e}", file=sys.stderr)
            time.sleep(3 * attempt)
            continue
        if r.status_code == 200 and r.text:
            return r.text
        print(f"  [jina {attempt}/3] {target_url} -> HTTP {r.status_code}", file=sys.stderr)
        time.sleep(3 * attempt)
    return None


def _parse_md(md: str) -> tuple[str, list[str]]:
    """从 Jina markdown 抽正文纯文本 + 图片 URL 列表。"""
    body_start = _JINA_BODY_RE.search(md)
    body = md[body_start.end():] if body_start else md

    images: list[str] = []
    for m in _IMG_RE.finditer(body):
        u = m.group(1)
        if u not in images and not u.lower().endswith(".svg"):
            images.append(u)
    images = images[:6]

    paras: list[str] = []
    for chunk in re.split(r"\n{2,}", body):
        p = chunk.strip()
        if not p:
            continue
        if p.startswith("!["):                      # 纯图片行
            continue
        if p.startswith("#"):                        # 标题行
            p = p.lstrip("# ").strip()
        if p.startswith(("Published Time:", "URL Source:", "Title:")):
            continue
        if _NAV_KEYWORDS_RE.search(p) and len(p) < 200:
            continue
        p = _MD_LINK_RE.sub(r"\1", p)                # 去 markdown 链接，仅留文字
        p = re.sub(r"[*_`>#]+", "", p)               # 去残留 md 记号
        p = re.sub(r"\s+", " ", p).strip()
        if len(p) >= 25:
            paras.append(p)
    text = "\n\n".join(paras[:40])
    return text, images


def main() -> int:
    ingest_url = os.environ.get("INGEST_URL", "").strip()
    token = os.environ.get("INGEST_TOKEN", "").strip()
    topic_id = os.environ.get("TOPIC_ID", "space-tug").strip()
    if not ingest_url or not token:
        print("Missing INGEST_URL / INGEST_TOKEN", file=sys.stderr)
        return 2

    root = ingest_url.split("/ingest/")[0].rstrip("/")
    topic_get = f"{root}/api/topic/get?id={topic_id}"
    topic_ingest = f"{root}/ingest/topic"
    print(f"root={root}\n get={topic_get}\n post={topic_ingest}")

    try:
        r = requests.get(topic_get, timeout=30)
        r.raise_for_status()
        items = r.json()["topic"]["items"]
    except Exception as e:
        print(f"fetch topic list failed: {e}", file=sys.stderr)
        return 3
    print(f"topic has {len(items)} items")

    out: list[dict] = []
    for it in items:
        url = it.get("url", "")
        if not url:
            continue
        print(f"\n--- {it.get('region','')} {it.get('title','')[:40]}\n    {url}")
        md = _fetch_via_jina(url)
        if not md:
            print("    jina failed, skip")
            continue
        text, images = _parse_md(md)
        print(f"    text={len(text)} chars, images={len(images)}")
        if len(text) < 200 and not images:
            print("    too short, skip")
            continue
        out.append({
            "url": url, "id": it.get("id", ""), "title": it.get("title", ""),
            "region": it.get("region", ""), "text": text, "images": images,
        })
        time.sleep(1.0)

    if not out:
        print("nothing scraped")
        return 0

    print(f"\nPOST {len(out)} items -> {topic_ingest}")
    resp = requests.post(
        topic_ingest,
        headers={"X-Auth-Token": token, "Content-Type": "application/json"},
        data=json.dumps({"articles": out}, ensure_ascii=False).encode("utf-8"),
        timeout=60,
    )
    print(resp.status_code, resp.text[:300])
    resp.raise_for_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
