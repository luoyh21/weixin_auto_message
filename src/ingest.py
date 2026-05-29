"""SpaceNews 远端 scraper 推送到本服务的入站缓存。

GitHub Actions 等海外抓取脚本可以 POST /ingest/spacenews，把抓到的文章
（已包含 title/link/published/content_html/image_url）落到 data/ingest/ 下，
daily 流程会优先从这里读取最近 N 小时的文章；缺失时再退回 spacelive 抓取。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import SETTINGS

log = logging.getLogger(__name__)

INGEST_DIR = SETTINGS.cache_dir.parent / "ingest"
INGEST_DIR.mkdir(parents=True, exist_ok=True)
INGEST_TOKEN_ENV = "SPACENEWS_INGEST_TOKEN"


def save_ingest(items: list[dict]) -> Path:
    ts = int(time.time())
    p = INGEST_DIR / f"spacenews_{ts}.json"
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("ingest saved %d items -> %s", len(items), p)
    return p


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(s)
        except Exception:
            return None


def load_recent(hours: int = 12) -> list[dict]:
    """合并 data/ingest/*.json，按 link 去重，按 published 过滤近 N 小时。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    merged: dict[str, dict] = {}
    for f in sorted(INGEST_DIR.glob("spacenews_*.json")):
        try:
            data = json.loads(f.read_text("utf-8"))
        except Exception as e:
            log.warning("bad ingest file %s: %s", f, e)
            continue
        for item in data:
            link = item.get("link", "")
            if not link:
                continue
            dt = _parse_dt(item.get("published", ""))
            if dt and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt is None or dt < cutoff:
                continue
            # 同 link 去重策略（文件按文件名升序遍历，最新的 ingest 文件最后处理）：
            #   ① published 更新的胜（罕见，因 RSS 时间稳定）
            #   ② published 相同时，**总是用更晚一次 ingest 的版本**——让远端
            #      脚本的逻辑迭代立即反映到本地，避免被旧文件 lock 住。
            cur = merged.get(link)
            if cur is None:
                merged[link] = item
            else:
                cur_dt = _parse_dt(cur.get("published", ""))
                if cur_dt is None or cur_dt <= dt:
                    merged[link] = item
    out = list(merged.values())
    out.sort(key=lambda x: _parse_dt(x.get("published", "")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    log.info("ingest load_recent(%dh): %d items", hours, len(out))
    return out


def has_recent(hours: int = 12) -> bool:
    return bool(load_recent(hours))
