"""政要社媒海外抓取脚本（在 GitHub Actions / 海外节点跑）。

抓两路，**可插拔**（任一路挂了不影响另一路）：

1. Truth Social（Trump）：读 stiles/trump-truth-social-archive 项目提供的公开存档
   JSON（CNN 托管，每 5 分钟更新）。数据结构沿用该项目：
       {id, created_at, content(HTML), url, media:[...]}
   零密钥、零代理。URL 可用 TRUTH_ARCHIVE_URL 覆盖。

2. X / 推特（Musk + 可选 Trump）：connectX 那套 nitter 多实例 RSS。
   逐实例尝试 https://<instance>/<user>/rss，命中即用，全挂则跳过。

两路统一规整成下面的 schema，POST 到国内服务的 /ingest/social：
    {
      "platform":    "x" | "truth_social",
      "author":      "elonmusk" | "realDonaldTrump",
      "author_name": "马斯克" | "特朗普",
      "post_id":     "...",
      "url":         "...",
      "published":   ISO8601,
      "text":        "原文纯文本",
      "images":      ["..."]
    }

相关性判定 / 翻译 / 解读都在服务端用 LLM 做（本脚本不带任何 key）。

环境变量：
    INGEST_URL        例 http://your.server.cn:8503/ingest/spacenews
                      （脚本会把路径换成 /ingest/social 复用同一台机器）
    INGEST_TOKEN      与服务端 SPACENEWS_INGEST_TOKEN 一致
    WINDOW_HOURS      仅推送过去 N 小时内的帖子，默认 26（日更留 2h 重叠）
    X_USERS           逗号分隔的 X 用户名，默认 elonmusk,realDonaldTrump
    TRUTH_USERS       逗号分隔的 Truth Social 用户名，默认 realDonaldTrump
    NITTER_INSTANCES  逗号分隔的 nitter 实例（不带末尾斜杠），可选
    TRUTH_ARCHIVE_URL 覆盖 CNN 存档地址，可选
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

import feedparser
import requests
from bs4 import BeautifulSoup

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36 Edg/123.0",
]

DEFAULT_NITTER = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://lightbrd.com",
    "https://nitter.tiekoetter.com",
]

DEFAULT_TRUTH_ARCHIVE = "https://ix.cnn.io/data/truth-social/truth_archive.json"

AUTHOR_NAMES = {
    "elonmusk": "马斯克",
    "realdonaldtrump": "特朗普",
}


def _author_name(user: str) -> str:
    return AUTHOR_NAMES.get(user.lower().lstrip("@"), user)


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return " ".join(BeautifulSoup(s, "html.parser").get_text(" ").split())


def _html_to_text(s: str) -> str:
    """HTML→纯文本，但**保留链接**：把 <a href="url">文本</a> 转成 "文本 url"。

    Truth Social 的 content 是带 <a> 的 HTML，直接 get_text 会丢掉 URL；
    这里把 href 显式拼回正文，方便 LLM 在译文里原样保留链接。
    """
    if not s:
        return ""
    soup = BeautifulSoup(s, "html.parser")
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        txt = a.get_text(" ").strip()
        if href and href not in txt:
            a.replace_with(f"{txt} {href}".strip())
    return " ".join(soup.get_text(" ").split())


_WORD_RE = re.compile(r"\S+")


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Truth Social（公开存档）
# --------------------------------------------------------------------------
def fetch_truth(users: list[str], cutoff: datetime, archive_url: str) -> list[dict]:
    out: list[dict] = []
    try:
        r = requests.get(
            archive_url,
            headers={"User-Agent": random.choice(USER_AGENTS), "Accept": "application/json"},
            timeout=40,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[truth] fetch failed: {e}", file=sys.stderr)
        return out

    if not isinstance(data, list):
        print(f"[truth] unexpected payload type: {type(data)}", file=sys.stderr)
        return out

    want = {u.lower().lstrip("@") for u in users}
    for post in data:
        if not isinstance(post, dict):
            continue
        url = post.get("url", "") or ""
        # 存档目前只含 realDonaldTrump；用 url 里的 @handle 兜底过滤
        m = re.search(r"@([A-Za-z0-9_]+)", url)
        handle = (m.group(1).lower() if m else "realdonaldtrump")
        if want and handle not in want:
            continue
        dt = _parse_iso(post.get("created_at", ""))
        if dt is None or dt < cutoff:
            continue
        text = _html_to_text(post.get("content", ""))
        media = post.get("media") or []
        images = [m for m in media if isinstance(m, str)
                  and not m.lower().split("?")[0].endswith((".mp4", ".mov", ".m4v", ".webm"))]
        if not text and not images:
            continue  # 纯视频/空帖，无可分析文本
        out.append({
            "platform": "truth_social",
            "author": handle,
            "author_name": _author_name(handle),
            "post_id": str(post.get("id", "")) or url,
            "url": url,
            "published": dt.isoformat(),
            "text": text,
            "images": images[:1],
        })
    print(f"[truth] {len(out)} posts in window")
    return out


# --------------------------------------------------------------------------
# X / 推特（nitter 多实例 RSS）
# --------------------------------------------------------------------------
def _fetch_nitter_rss(instance: str, user: str) -> bytes | None:
    url = f"{instance.rstrip('/')}/{user}/rss"
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
            timeout=25,
        )
    except requests.RequestException as e:
        print(f"  [x] {url} -> {e}", file=sys.stderr)
        return None
    if r.status_code == 200 and r.content and b"<rss" in r.content[:2000].lower():
        return r.content
    print(f"  [x] {url} -> HTTP {r.status_code}", file=sys.stderr)
    return None


def _img_from_summary(html: str) -> list[str]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if src and "emoji" not in src.lower() and "avatar" not in src.lower():
            out.append(src)
    return out


def fetch_x(users: list[str], cutoff: datetime, instances: list[str]) -> list[dict]:
    out: list[dict] = []
    for user in users:
        user = user.strip().lstrip("@")
        if not user:
            continue
        feed_bytes = None
        for inst in instances:
            feed_bytes = _fetch_nitter_rss(inst, user)
            if feed_bytes:
                print(f"[x] {user}: hit {inst}")
                break
            time.sleep(random.uniform(0.5, 1.5))
        if not feed_bytes:
            print(f"[x] {user}: all nitter instances failed", file=sys.stderr)
            continue
        parsed = feedparser.parse(feed_bytes)
        n = 0
        for entry in parsed.entries:
            t = entry.get("published_parsed") or entry.get("updated_parsed")
            if not t:
                continue
            dt = datetime(*t[:6], tzinfo=timezone.utc)
            if dt < cutoff:
                continue
            link = entry.get("link", "").strip()
            raw = _strip_html(entry.get("title", "")) or _strip_html(entry.get("summary", ""))
            low = raw.lower()
            if low.startswith("rt by"):
                continue  # 纯转推不是本人言论，跳过
            text = raw
            is_reply = low.startswith("r to @")
            if is_reply:
                # 去掉 "R to @user:" 前缀；回复需正文 >30 词才纳入考虑
                text = re.sub(r"^R to @\S+:?\s*", "", raw, flags=re.I).strip()
                if _word_count(text) <= 30:
                    continue
            images = _img_from_summary(entry.get("summary", ""))
            if not text and not images:
                continue
            pid = link.split("/")[-1].split("#")[0] or link
            out.append({
                "platform": "x",
                "author": user,
                "author_name": _author_name(user),
                "post_id": pid,
                "url": link,
                "published": dt.isoformat(),
                "text": text,
                "images": images[:1],
            })
            n += 1
        print(f"[x] {user}: +{n} posts in window (feed had {len(parsed.entries)} entries)")
    return out


def main() -> int:
    ingest_url = os.environ.get("INGEST_URL")
    token = os.environ.get("INGEST_TOKEN")
    if not ingest_url or not token:
        print("Missing INGEST_URL / INGEST_TOKEN", file=sys.stderr)
        return 2
    social_url = ingest_url.split("/ingest/")[0] + "/ingest/social"

    hours = int(os.environ.get("WINDOW_HOURS", "26"))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    x_users = [u for u in os.environ.get("X_USERS", "elonmusk,realDonaldTrump").split(",") if u.strip()]
    truth_users = [u for u in os.environ.get("TRUTH_USERS", "realDonaldTrump").split(",") if u.strip()]
    instances = [i.strip() for i in os.environ.get("NITTER_INSTANCES", "").split(",") if i.strip()] or DEFAULT_NITTER
    archive_url = os.environ.get("TRUTH_ARCHIVE_URL", DEFAULT_TRUTH_ARCHIVE)

    state_path = Path(__file__).with_suffix(".state.json")
    seen: set[str] = set()
    if state_path.exists():
        try:
            seen = set(json.loads(state_path.read_text("utf-8")).get("ids", []))
        except Exception:
            seen = set()

    posts: list[dict] = []
    posts += fetch_truth(truth_users, cutoff, archive_url)
    posts += fetch_x(x_users, cutoff, instances)

    # 去重（platform:post_id），并跳过历史已推送的
    fresh: list[dict] = []
    batch_seen: set[str] = set()
    for p in posts:
        key = f"{p['platform']}:{p['post_id']}"
        if key in seen or key in batch_seen:
            continue
        batch_seen.add(key)
        fresh.append(p)

    if not fresh:
        print("No new social posts in window.")
        return 0

    print(f"POST {len(fresh)} posts -> {social_url}")
    resp = requests.post(
        social_url,
        headers={"X-Auth-Token": token, "Content-Type": "application/json"},
        data=json.dumps({"posts": fresh}, ensure_ascii=False).encode("utf-8"),
        timeout=40,
    )
    print(resp.status_code, resp.text[:400])
    resp.raise_for_status()

    seen.update(f"{p['platform']}:{p['post_id']}" for p in fresh)
    state_path.write_text(json.dumps({"ids": list(seen)[-1000:]}, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
