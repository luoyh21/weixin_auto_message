"""独立的公众号文章库（与「推送去重」「每日缓存」解耦）。

背景
----
小程序原本只读 ``data/cache/*.json`` 里的 ``opml[]``，而该字段是
``run_daily()``（仅早间一次）经过**推送去重 dedup** 后的结果。任何被
dedup 过滤掉、或因 zlzchat 聚合延迟当次没抓到的公众号更新，都会永久
不出现在小程序里。

本模块提供一个「抓到即入库」的持久存储：
- 抓取（daily/定时任务）拿到 OPML 条目后**立即**写入本库（dedup 之前）；
- 以文章链接为唯一键去重（只保证库内不重复，不影响推送可见性）；
- 小程序读取时把本库与 cache 合并，从而**每条抓到过的公众号更新都可见**。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import news_archive

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_STORE = _ROOT / "data" / "gzh_store.json"
_LOCK = threading.Lock()

# 保留多久（天）。小程序看近两周，留足冗余，但避免无限增长。
RETENTION_DAYS = 14


def _key(link: str, title: str = "") -> str:
    raw = (link or title or "").strip()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _load() -> dict[str, dict]:
    if not _STORE.exists():
        return {}
    try:
        data = json.loads(_STORE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as e:
        log.warning("gzh_store load failed: %s", e)
    return {}


def _save(d: dict[str, dict]) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STORE)


def add(entries: list[dict]) -> int:
    """把抓到的公众号条目并入库，按 link 去重。返回新增条数。

    每个 entry 至少含 ``link``，可含 source/title/published/description/image_url。
    已存在的条目仅在新值更优时补全 description / image_url。
    """
    if not entries:
        return 0
    # 先永久归档原始抓取结果；即便短期展示库随后按 14 天轮转，记录仍可供后续使用。
    news_archive.append("gzh", entries)
    with _LOCK:
        store = _load()
        added = 0
        for e in entries:
            link = (e.get("link") or "").strip()
            title = (e.get("title") or "").strip()
            if not link and not title:
                continue
            k = _key(link, title)
            cur = store.get(k)
            if cur is None:
                store[k] = {
                    "source": e.get("source") or "公众号",
                    "title": title,
                    "link": link,
                    "published": e.get("published") or "",
                    "description": e.get("description") or "",
                    "image_url": e.get("image_url") or "",
                    "first_seen": _now_utc().isoformat(timespec="seconds"),
                }
                added += 1
            else:
                # 补全更优字段（更长的描述 / 缺失的图）
                nd = e.get("description") or ""
                if len(nd) > len(cur.get("description") or ""):
                    cur["description"] = nd
                if not cur.get("image_url") and e.get("image_url"):
                    cur["image_url"] = e.get("image_url")
                if not cur.get("published") and e.get("published"):
                    cur["published"] = e.get("published")
        _prune(store)
        _save(store)
        if added:
            log.info("gzh_store: +%d new (total=%d)", added, len(store))
        return added


def _prune(store: dict[str, dict]) -> None:
    cutoff = _now_utc() - timedelta(days=RETENTION_DAYS)
    drop = []
    for k, v in store.items():
        ref = _parse_dt(v.get("published") or "") or _parse_dt(v.get("first_seen") or "")
        if ref and ref < cutoff:
            drop.append(k)
    for k in drop:
        store.pop(k, None)


def refresh(hours: int = 72, fetch_desc: bool = True) -> int:
    """主动抓一次 OPML 并入库（供定时任务调用）。返回新增条数。"""
    from .opml_feeds import fetch_opml_recent

    try:
        entries = [e.to_dict() for e in fetch_opml_recent(hours=hours, fetch_desc=fetch_desc)]
    except Exception as e:
        log.exception("gzh_store refresh fetch failed: %s", e)
        return 0
    return add(entries)


def load_recent(days: int = 9) -> list[dict]:
    """返回近 ``days`` 天的公众号条目（按发布时间倒序），字段与 OPML 条目一致。

    时间基准优先用 published，缺失时退化到 first_seen，保证抓到就能展示。
    """
    store = _load()
    cutoff = _now_utc() - timedelta(days=days)
    out: list[dict] = []
    for v in store.values():
        ref = _parse_dt(v.get("published") or "") or _parse_dt(v.get("first_seen") or "")
        if ref is None or ref < cutoff:
            continue
        out.append({
            "source": v.get("source") or "公众号",
            "title": v.get("title") or "",
            "link": v.get("link") or "",
            "published": v.get("published") or v.get("first_seen") or "",
            "description": v.get("description") or "",
            "image_url": v.get("image_url") or "",
        })
    out.sort(key=lambda x: _parse_dt(x["published"]) or _now_utc(), reverse=True)
    return out
