"""周期性清理：只保留近 N 天（默认 30，约一个月）的中间文件/缓存。

涉及目录（按文件 mtime 判断）：
    data/ingest/*.json            远端 scraper 推上来的原始抓取批
    data/cache/*.json             daily 汇总缓存
    data/translate_cache/*.txt    GPT 翻译片段缓存
    data/img_cache/*.{bin,ct}     /img 代理的图片缓存
    data/dy_pages/*.html          抖音落地页
    data/news_pages/<batch>/*     国际新闻翻译页（同时按 manifest 也轮转，这里兜底清孤儿目录）

不动的目录：
    data/join                     扫码引导页所需 PNG/JSON
    data/zlzchat.opml             订阅源
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _prune_files(folder: Path, *, patterns: tuple[str, ...], days: int) -> int:
    if not folder.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for pat in patterns:
        for p in folder.glob(pat):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except Exception as e:
                log.warning("rm %s failed: %s", p, e)
    if removed:
        log.info("cleanup: %s removed %d files (>%dd)", folder.name, removed, days)
    return removed


def _prune_subdirs(folder: Path, *, days: int) -> int:
    """对 news_pages/<batch>/ 这类按子目录组织的内容，按子目录 mtime 整体清理。"""
    if not folder.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for sub in folder.iterdir():
        if not sub.is_dir():
            continue
        try:
            if sub.stat().st_mtime < cutoff:
                shutil.rmtree(sub, ignore_errors=True)
                removed += 1
        except Exception as e:
            log.warning("rmtree %s failed: %s", sub, e)
    if removed:
        log.info("cleanup: %s removed %d sub-dirs (>%dd)", folder.name, removed, days)
    return removed


def run(days: int = 30) -> dict:
    """主入口：清理所有受管目录，返回每个目录删除计数。"""
    stats = {
        "ingest":          _prune_files(DATA / "ingest",          patterns=("*.json",),       days=days),
        "cache":           _prune_files(DATA / "cache",           patterns=("*.json",),       days=days),
        "translate_cache": _prune_files(DATA / "translate_cache", patterns=("*.txt",),        days=days),
        "img_cache":       _prune_files(DATA / "img_cache",       patterns=("*.bin", "*.ct"), days=days),
        "dy_pages":        _prune_files(DATA / "dy_pages",        patterns=("*.html",),       days=days),
        "news_pages":      _prune_subdirs(DATA / "news_pages",                                days=days),
    }
    # 政要社媒库按发布时间自带滚动清理（RETENTION_DAYS），这里再触发一次兜底。
    try:
        from .social_store import prune as _social_prune
        stats["social_store"] = _social_prune()
    except Exception as e:
        log.warning("social_store prune failed: %s", e)
    log.info("cleanup summary (keep %dd): %s", days, stats)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    run()
