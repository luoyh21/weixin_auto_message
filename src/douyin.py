"""抖音用户主页作品抓取（依赖本机 evil0ctal/douyin_tiktok_download_api 容器）。"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "http://127.0.0.1:8504"


@dataclass
class DouyinEntry:
    source: str          # 账号显示名
    sec_user_id: str
    aweme_id: str
    title: str           # 取 desc 首行/截断
    link: str            # 可直接打开的分享链接
    image_url: str       # 视频封面
    published: str       # ISO 时间
    create_ts: int
    share_url: str = ""        # 原始 share_url（长链）
    share_text: str = ""       # 抖音口令完整文本（已替换 %s 为短链/长链）
    desc: str = ""             # 原始 desc 全文

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_users(raw: str) -> list[tuple[str, str]]:
    """解析 DOUYIN_USERS。

    支持两种条目格式（多个用英文逗号分隔）：
        我们的太空:MS4wLjABAAAA8tYhNulGyT_4NVlSylLBZKvSEkqACthevMPPXbTZgXI
        MS4wLjABAAAA8tYhNulGyT_4NVlSylLBZKvSEkqACthevMPPXbTZgXI
    返回 [(name, sec_user_id), ...]
    """
    out: list[tuple[str, str]] = []
    if not raw:
        return out
    for piece in raw.split(","):
        s = piece.strip()
        if not s:
            continue
        if ":" in s:
            name, sec = s.split(":", 1)
            name, sec = name.strip(), sec.strip()
        else:
            name, sec = s, s
        if sec:
            out.append((name or sec, sec))
    return out


def _short_title(desc: str, max_len: int = 60) -> str:
    desc = (desc or "").strip()
    if not desc:
        return "（无标题）"
    line = desc.splitlines()[0].strip()
    return line[:max_len] + ("…" if len(line) > max_len else "")


def _share_link(aweme_id: str) -> str:
    """返回稳定的短链，企业微信卡片点击直达。"""
    return f"https://www.iesdouyin.com/share/video/{aweme_id}/"


def _pick_cover(video: dict | None) -> str:
    if not video:
        return ""
    cover = video.get("cover") or {}
    urls = cover.get("url_list") or []
    return urls[0] if urls else ""


def fetch_user_recent(
    sec_user_id: str,
    *,
    name: str = "",
    hours: int = 24,
    count: int = 20,
    api_base: str | None = None,
    timeout: float = 15.0,
) -> list[DouyinEntry]:
    base = (api_base or os.getenv("DOUYIN_API_BASE", DEFAULT_API_BASE)).rstrip("/")
    url = f"{base}/api/douyin/web/fetch_user_post_videos"
    params = {"sec_user_id": sec_user_id, "max_cursor": 0, "count": count}
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        log.warning("douyin fetch failed for %s (%s): %s", name or sec_user_id, sec_user_id, e)
        return []

    data = (payload or {}).get("data") or {}
    aweme_list = data.get("aweme_list") or []

    cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
    entries: list[DouyinEntry] = []
    for a in aweme_list:
        if a.get("is_top"):  # 跳过置顶（不算"最近作品"）
            continue
        ts = a.get("create_time")
        if not ts or ts < cutoff_ts:
            continue
        aweme_id = str(a.get("aweme_id") or "").strip()
        if not aweme_id:
            continue
        share_url = a.get("share_url") or _share_link(aweme_id)
        share_info = a.get("share_info") or {}
        raw_desc = a.get("share_link_desc") or share_info.get("share_link_desc") or ""
        # share_link_desc 里有 %s 占位，原本要替换为短链；抖音 App 实际识别的是其中的口令字符（如 "D@H.vf"），URL 用长链亦可
        share_text = raw_desc.replace("%s", share_url) if raw_desc else ""
        entries.append(
            DouyinEntry(
                source=name or sec_user_id,
                sec_user_id=sec_user_id,
                aweme_id=aweme_id,
                title=_short_title(a.get("desc") or a.get("caption") or ""),
                link=_share_link(aweme_id),
                image_url=_pick_cover(a.get("video")),
                published=datetime.fromtimestamp(ts, tz=timezone.utc)
                .astimezone()
                .isoformat(timespec="seconds"),
                create_ts=int(ts),
                share_url=share_url,
                share_text=share_text,
                desc=(a.get("desc") or "").strip(),
            )
        )
    entries.sort(key=lambda e: e.create_ts, reverse=True)
    log.info("douyin %s -> %d entries within %dh", name or sec_user_id, len(entries), hours)
    return entries


def fetch_douyin_recent(
    users_raw: str | None = None,
    *,
    hours: int = 24,
    max_total: int = 2,
    per_user_limit: int = 1,
    per_user_fetch: int = 20,
) -> list[DouyinEntry]:
    """聚合所有账号的近 N 小时作品。

    规则：每个账号最多 `per_user_limit` 条（默认 1，即"只取最新一条"），
         然后按时间倒序合并，截断到全局 `max_total`。
         不在时间窗口内的账号直接跳过、不占名额。
    """
    users = _parse_users(users_raw or os.getenv("DOUYIN_USERS", ""))
    if not users or max_total <= 0:
        return []

    collected: list[DouyinEntry] = []
    for name, sec in users:
        items = fetch_user_recent(sec, name=name, hours=hours, count=per_user_fetch)
        collected.extend(items[:max(1, per_user_limit)])

    collected.sort(key=lambda e: e.create_ts, reverse=True)
    return collected[:max_total]
