"""永久新闻归档（不参与推送、小程序展示或日常清理）。

抓到的新闻在进入短期缓存/滚动 store 时，同时按来源追加到
``data/news_archive/<kind>/<YYYY>/<MM>.jsonl``。每行一条 JSON，保留原始
字段与归档时间；同一来源内以稳定指纹去重。归档目录刻意不被 cleanup 管理，
供后续检索、专题整理或离线分析使用。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = ROOT / "data" / "news_archive"
_LOCK = threading.Lock()


def _key(kind: str, item: dict) -> str:
    """优先用可复现的业务唯一键，缺失时再对整条记录做哈希。"""
    for name in ("link", "url", "post_id", "project_id", "launch_id", "date", "id", "title"):
        value = item.get(name)
        if value:
            return f"{kind}:{name}:{value}"
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    return f"{kind}:sha1:{hashlib.sha1(raw.encode('utf-8')).hexdigest()}"


def append(kind: str, items: list[dict]) -> int:
    """把新抓到的记录永久追加归档，返回实际新增数。

    JSONL 文件中的 ``_archive_key`` 既是去重索引，也让归档文件本身可独立使用。
    单月内先扫描既有 key；日常增量很小，避免额外维护容易损坏的索引文件。
    """
    if not items:
        return 0
    now = datetime.now(timezone.utc)
    path = ARCHIVE_DIR / kind / now.strftime("%Y") / f"{now:%m}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        existing: set[str] = set()
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            existing.add(json.loads(line).get("_archive_key", ""))
                        except Exception:
                            continue
        except Exception as e:
            log.warning("news archive read failed %s: %s", path, e)

        rows: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = _key(kind, item)
            if key in existing:
                continue
            row = dict(item)
            row["_archive_key"] = key
            row["_archived_at"] = now.isoformat(timespec="seconds")
            rows.append(json.dumps(row, ensure_ascii=False, default=str))
            existing.add(key)

        if not rows:
            return 0
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
    log.info("news archive: +%d %s -> %s", len(rows), kind, path.relative_to(ROOT))
    return len(rows)
