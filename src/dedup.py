"""推送去重：跨推送（不带上半天/前一天）+ 同次推送内跨信源去重。

机制：
- data/push_history.json 记录 last_push_at（上次成功推送时间）与最近若干天已推条目
  （原始链接 + 归一化标题）。
- filter_new：剔除「发布时间 ≤ 上次推送时间」「链接已推过」「标题与历史高度雷同」的条目。
- dedup_within：同一次推送内，按标题相似度去掉跨信源的高度雷同条目。
- record：成功发送后写入本次新条目，并把 last_push_at 置为当前时间（这样上半天窗口内
  未被选中的条目，下半天也不会再出现）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

from .config import SETTINGS
from .news_pages import _parse_dt

log = logging.getLogger(__name__)

HISTORY_FILE = SETTINGS.cache_dir.parent / "push_history.json"
KEEP_DAYS = 4
TITLE_DUP_THR = 0.68   # 与历史标题的相似度阈值
WITHIN_DUP_THR = 0.62  # 同次推送内跨源相似度阈值（语序无关的 token 重合更敏感）


def _load() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"last_push_at": None, "items": []}


def last_push_at() -> datetime | None:
    s = _load().get("last_push_at")
    return _parse_dt(s) if s else None


def _norm(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", t)
    return " ".join(t.split())


def _jaccard(a: str, b: str) -> float:
    """词集合 Jaccard 相似度（语序无关，适合跨信源同事件的英文标题）。"""
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / len(sa | sb)


def _sim(a: str, b: str) -> float:
    """综合相似度：取「连续匹配比」与「词集合 Jaccard」的较大值。"""
    if not a or not b:
        return 0.0
    return max(SequenceMatcher(None, a, b).ratio(), _jaccard(a, b))


def filter_new(
    items: list[dict],
    *,
    link_key: str = "link",
    title_key: str = "title",
    pub_key: str = "published",
) -> list[dict]:
    """剔除已推过 / 上次推送之前 / 与历史标题高度雷同的条目。"""
    d = _load()
    cutoff = last_push_at()
    hist_links = {i.get("link") for i in d.get("items", []) if i.get("link")}
    hist_norms = [i.get("norm") for i in d.get("items", []) if i.get("norm")]
    kept: list[dict] = []
    for a in items:
        link = a.get(link_key) or ""
        pub = _parse_dt(a.get(pub_key) or "")
        if cutoff and pub and pub <= cutoff:
            continue
        if link and link in hist_links:
            continue
        norm = _norm(a.get(title_key))
        if norm and any(_sim(norm, h) >= TITLE_DUP_THR for h in hist_norms):
            log.info("dedup vs history (title): %s", (a.get(title_key) or "")[:50])
            continue
        kept.append(a)
    return kept


def dedup_within(items: list[dict], *, title_key: str = "title") -> list[dict]:
    """同一次推送内按标题相似度去重（跨信源高度雷同只留第一条）。"""
    out: list[dict] = []
    norms: list[str] = []
    for a in items:
        n = _norm(a.get(title_key))
        if n and any(_sim(n, m) >= WITHIN_DUP_THR for m in norms):
            log.info("dedup within batch: %s", (a.get(title_key) or "")[:50])
            continue
        out.append(a)
        norms.append(n)
    return out


def record(items: list[dict], *, link_key: str = "link", title_key: str = "title") -> None:
    """成功发送后写入历史，并把 last_push_at 置为当前时间；按 KEEP_DAYS 清理。"""
    d = _load()
    now = datetime.now(timezone.utc)
    for a in items:
        link = a.get("original_link") or a.get(link_key) or ""
        d["items"].append({
            "link": link,
            "norm": _norm(a.get("title_zh") or a.get(title_key)),
            "at": now.isoformat(),
        })
    cut = now - timedelta(days=KEEP_DAYS)
    d["items"] = [i for i in d["items"] if (_parse_dt(i.get("at") or "") or now) >= cut]
    d["last_push_at"] = now.isoformat()
    HISTORY_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("push_history updated: +%d items, last_push_at=%s", len(items), d["last_push_at"])
