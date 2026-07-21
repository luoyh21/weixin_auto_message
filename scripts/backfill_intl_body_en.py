#!/usr/bin/env python3
"""为近 N 天国际要闻 cache 回填英文原文（body_en / summary_en）。

优先从 content_html 抽正文；没有 HTML 时用 RSS summary 作为 summary_en。
不调用 LLM。

用法：
  .venv/bin/python scripts/backfill_intl_body_en.py [--days 14]
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

from src.news_pages import _extract_main_html  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_intl_body_en")
CACHE_DIR = ROOT / "data" / "cache"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    today = date.today()
    wrote = skipped = 0
    for i in range(args.days):
        d = (today - timedelta(days=i)).isoformat()
        for sess in ("morning", "evening"):
            path = CACHE_DIR / f"{sess}_{d}.json"
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            changed = False
            for a in data.get("spacenews") or []:
                if not (a.get("summary_en") or "").strip() and (a.get("summary") or "").strip():
                    a["summary_en"] = a["summary"]
                    changed = True
                if (a.get("body_en") or "").strip():
                    skipped += 1
                    continue
                html = a.get("content_html") or ""
                url = a.get("original_link") or a.get("link") or ""
                if not html:
                    continue
                try:
                    text, _, _ = _extract_main_html(html, url)
                except Exception as e:
                    log.warning("extract failed %s: %s", url, e)
                    continue
                text = (text or "").strip()
                if not text:
                    continue
                a["body_en"] = text
                changed = True
                wrote += 1
            if changed:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                log.info("saved %s", path.name)
    log.info("done: wrote body_en=%d skipped_existing=%d", wrote, skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
