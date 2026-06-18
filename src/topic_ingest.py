"""专题情报的「海外抓取入站缓存」。

GitHub Actions（海外 runner）用 Jina Reader 抓取专题条目全文 + 图片后，
POST 到本服务 /ingest/topic，按条目 url 落到 data/ingest/topic_*.json。
weixin_miniprogram 的 topic_intel 在重新生成专题时优先读取这里的全文，
绕开国内直连被 Cloudflare/限流/PDF 拦截的源。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .config import SETTINGS

log = logging.getLogger(__name__)

INGEST_DIR = SETTINGS.cache_dir.parent / "ingest"
INGEST_DIR.mkdir(parents=True, exist_ok=True)


def save_ingest(items: list[dict]) -> Path:
    ts = int(time.time())
    p = INGEST_DIR / f"topic_{ts}.json"
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("topic ingest saved %d items -> %s", len(items), p)
    return p


def load_map() -> dict[str, dict]:
    """合并 data/ingest/topic_*.json，按 url 去重（后写入的覆盖先写入的）。"""
    merged: dict[str, dict] = {}
    for f in sorted(INGEST_DIR.glob("topic_*.json")):
        try:
            data = json.loads(f.read_text("utf-8"))
        except Exception as e:
            log.warning("bad topic ingest file %s: %s", f, e)
            continue
        for item in data:
            url = (item.get("url") or item.get("link") or "").strip()
            if url:
                merged[url] = item
    return merged
