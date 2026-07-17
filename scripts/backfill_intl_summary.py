#!/usr/bin/env python3
"""为近 N 天国际要闻补写 summary_zh，并重建翻译页 HTML。

用法：
  .venv/bin/python scripts/backfill_intl_summary.py [--days 3] [--force]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.news_pages import rebuild_pages_from_cache  # noqa: E402
from src.summarizer import summarize_zh  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_intl_summary")

CACHE_DIR = ROOT / "data" / "cache"


def _iter_cache_files(days: int) -> list[Path]:
    today = date.today()
    out: list[Path] = []
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        for sess in ("morning", "evening"):
            p = CACHE_DIR / f"{sess}_{d}.json"
            if p.exists():
                out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="回填最近几天（含今天）")
    ap.add_argument("--force", action="store_true", help="已有 summary_zh 也重写")
    args = ap.parse_args()

    files = _iter_cache_files(args.days)
    if not files:
        log.warning("no cache files in last %d days", args.days)
        return 0

    total = wrote = skipped = failed = 0
    rebuilt_articles: list[dict] = []

    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        sn = data.get("spacenews") or []
        changed = False
        for a in sn:
            body = (a.get("body_zh") or "").strip()
            if not body:
                continue
            total += 1
            if (a.get("summary_zh") or "").strip() and not args.force:
                skipped += 1
                rebuilt_articles.append(a)
                continue
            title = (a.get("title_zh") or a.get("title") or "").strip()
            try:
                blurb = summarize_zh(title, body)
            except Exception as e:
                log.exception("summarize failed: %s", a.get("link"))
                failed += 1
                continue
            if not blurb:
                failed += 1
                continue
            a["summary_zh"] = blurb
            changed = True
            wrote += 1
            rebuilt_articles.append(a)
            log.info("[%s] %s → %s", path.name, title[:36], blurb[:60])
        if changed:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("saved %s", path.name)

    n_pages = rebuild_pages_from_cache(rebuilt_articles)
    log.info(
        "done: candidates=%d wrote=%d skipped=%d failed=%d pages=%d",
        total, wrote, skipped, failed, n_pages,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
