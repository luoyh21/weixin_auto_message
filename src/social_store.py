"""政要社媒（X / Truth Social）独立存储 + LLM 富化。

与每日推送解耦，单独维护一份 data/social_store.json：
- 海外 GH Actions 抓到的原始帖子 POST 到 /ingest/social；
- 服务端对每条**新**帖做 LLM 相关性判定：与航天器完全无关的直接丢弃、不入库；
- 相关的翻译成中文 + 给出航天视角解读，连同时间/渠道/原文/图片一起入库；
- 小程序后端从这里读取，渲染「政要社媒」栏目。

存储为按 key=``platform:post_id`` 索引的 dict，自带 RETENTION_DAYS 滚动清理。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STORE_PATH = ROOT / "data" / "social_store.json"
STORE_PATH.parent.mkdir(parents=True, exist_ok=True)

RETENTION_DAYS = 15
MAX_PER_INGEST = 40  # 单次入库最多富化多少条，挡住马斯克高频刷屏导致的 LLM 费用失控

_LOCK = threading.Lock()

CHANNEL_LABEL = {
    "x": "X（推特）",
    "truth_social": "Truth Social",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _key(post: dict) -> str:
    return f"{post.get('platform','')}:{post.get('post_id','')}"


def _load() -> dict[str, dict]:
    if not STORE_PATH.exists():
        return {}
    try:
        return json.loads(STORE_PATH.read_text("utf-8"))
    except Exception as e:
        log.warning("social_store load failed: %s", e)
        return {}


def _save(store: dict[str, dict]) -> None:
    tmp = STORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE_PATH)


def _prune(store: dict[str, dict]) -> None:
    cutoff = _now() - timedelta(days=RETENTION_DAYS)
    for k in list(store.keys()):
        dt = _parse_iso(store[k].get("published", ""))
        if dt is None or dt < cutoff:
            store.pop(k, None)


def ingest_and_enrich(posts: list[dict]) -> int:
    """对一批原始帖子做去重 + LLM 富化后入库。返回实际新增（相关且入库）的条数。

    每条只在**首次见到**时调用一次 LLM；相关性为 False 的不入库（但记入 skip 以免重复判定）。
    """
    from .summarizer import analyze_social_post

    with _LOCK:
        store = _load()
        seen_keys = set(store.keys())

    # 仅保留库里没有、且本批不重复的，按时间倒序，截断到 MAX_PER_INGEST
    fresh: list[dict] = []
    batch_seen: set[str] = set()
    for p in posts:
        k = _key(p)
        if not p.get("post_id") or k in seen_keys or k in batch_seen:
            continue
        batch_seen.add(k)
        fresh.append(p)
    fresh.sort(key=lambda x: _parse_iso(x.get("published", "")) or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    fresh = fresh[:MAX_PER_INGEST]

    added = 0
    enriched: dict[str, dict] = {}
    for p in fresh:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        res = analyze_social_post(p.get("author_name", ""), text, p.get("platform", ""))
        if not res.get("relevant"):
            log.info("social skip (irrelevant): %s %s", p.get("author_name"), text[:50])
            continue
        images = p.get("images") or []
        enriched[_key(p)] = {
            "platform": p.get("platform", ""),
            "channel": CHANNEL_LABEL.get(p.get("platform", ""), p.get("platform", "")),
            "author": p.get("author", ""),
            "author_name": p.get("author_name", ""),
            "post_id": p.get("post_id", ""),
            "url": p.get("url", ""),
            "published": p.get("published", ""),
            "title": res.get("title") or f"{p.get('author_name','')}最新动态",
            "original": text,
            "translation": res.get("translation", ""),
            "analysis": res.get("analysis", ""),
            "image": images[0] if images else "",
            "first_seen": _now().isoformat(),
        }
        added += 1

    if enriched:
        with _LOCK:
            store = _load()
            store.update(enriched)
            _prune(store)
            _save(store)
    log.info("social ingest: %d posts in, %d relevant stored", len(posts), added)
    return added


def load_recent(days: int = 9) -> list[dict]:
    """返回近 ``days`` 天的政要社媒条目，按发布时间倒序。"""
    cutoff = _now() - timedelta(days=days)
    store = _load()
    out = []
    for v in store.values():
        dt = _parse_iso(v.get("published", ""))
        if dt is None or dt < cutoff:
            continue
        out.append(v)
    out.sort(key=lambda x: _parse_iso(x.get("published", "")) or datetime.min.replace(tzinfo=timezone.utc),
             reverse=True)
    return out


def prune() -> int:
    """供定时清理调用。返回剩余条数。"""
    with _LOCK:
        store = _load()
        before = len(store)
        _prune(store)
        _save(store)
    log.info("social_store prune: %d -> %d", before, len(store))
    return len(store)
