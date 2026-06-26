"""技术港：NASA TechPort 每日更新项目库。

数据源（国内服务器可直连）：
- 列表：https://techport.nasa.gov/api/projects?updatedSince=YYYY-MM-DD
        返回 {"projects":[{"projectId":..,"lastUpdated":".."}, ...]}
- 详情：https://techport.nasa.gov/api/projects/<id>
        返回 {"project":{title, description(HTML), benefits, status, lastUpdated, ...}}

每个「更新过的项目」入库一条，标题/摘要经 LLM 译成中文。结构对齐 gzh_store：
_load/_save/add/_prune/load_recent + refresh()，落 data/techport_store.json，保留 14 天。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import requests

from pathlib import Path

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_STORE = _ROOT / "data" / "techport_store.json"
_LOCK = threading.Lock()

RETENTION_DAYS = 14
LOOKBACK_DAYS = 7  # 每次回看窗口：TechPort 更新有滞后，固定窗口+去重最稳
MAX_PER_RUN = 25  # 单次最多取多少个更新项目，控 LLM 成本
_LIST_URL = "https://techport.nasa.gov/api/projects"
_DETAIL_URL = "https://techport.nasa.gov/api/projects/{pid}"
_VIEW_URL = "https://techport.nasa.gov/view/{pid}"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", s).strip()


def _parse_updated(s: str) -> datetime | None:
    """TechPort 的 lastUpdated 有两种格式：列表 '2026-6-23'、详情 '06/23/26'。"""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _load() -> dict[str, dict]:
    if not _STORE.exists():
        return {}
    try:
        d = json.loads(_STORE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception as e:
        log.warning("techport_store load failed: %s", e)
    return {}


def _save(d: dict[str, dict]) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STORE)


def _prune(store: dict[str, dict]) -> None:
    cutoff = _now_utc() - timedelta(days=RETENTION_DAYS)
    for k in [k for k, v in store.items()
              if (_parse_updated(v.get("updated_raw") or "")
                  or _now_utc()) < cutoff]:
        store.pop(k, None)


def _fetch_detail(pid: int) -> dict | None:
    try:
        r = requests.get(_DETAIL_URL.format(pid=pid),
                         headers={"User-Agent": _UA, "Accept": "application/json"},
                         timeout=25)
        r.raise_for_status()
        d = r.json()
        return d.get("project") or d
    except Exception as e:
        log.warning("techport detail %s failed: %s", pid, e)
        return None


def refresh() -> int:
    """抓一次 TechPort 更新项目并入库（译中文）。返回新增/更新条数。"""
    from .summarizer import translate_zh

    # 固定回看 LOOKBACK_DAYS 天（TechPort 更新有滞后，移动游标会漏）；去重靠 projectId+lastUpdated
    since = (_now_utc() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    try:
        r = requests.get(_LIST_URL, params={"updatedSince": since},
                         headers={"User-Agent": _UA, "Accept": "application/json"},
                         timeout=30)
        r.raise_for_status()
        projects = r.json().get("projects") or []
    except Exception as e:
        log.exception("techport list fetch failed: %s", e)
        return 0

    with _LOCK:
        store = _load()
        # 仅处理「新出现或 lastUpdated 变化」的项目，最多 MAX_PER_RUN 个
        todo = []
        for p in projects:
            pid = p.get("projectId")
            if pid is None:
                continue
            key = str(pid)
            upd = str(p.get("lastUpdated") or "")
            cur = store.get(key)
            if cur is None or cur.get("updated_raw") != upd:
                todo.append((pid, upd))
        # 较新的优先（按 lastUpdated 倒序）
        todo.sort(key=lambda x: _parse_updated(x[1]) or _now_utc(), reverse=True)
        todo = todo[:MAX_PER_RUN]

        added = 0
        for pid, upd in todo:
            detail = _fetch_detail(pid)
            if not detail:
                continue
            title_en = (detail.get("title") or "").strip()
            desc_en = _strip_html(detail.get("description") or "")
            if not title_en and not desc_en:
                continue
            zh = translate_zh(title_en, desc_en)
            upd_raw = str(detail.get("lastUpdated") or upd)
            dt = _parse_updated(upd_raw) or _now_utc()
            store[str(pid)] = {
                "project_id": pid,
                "title": zh.get("title") or title_en,
                "title_en": title_en,
                "summary": zh.get("summary") or desc_en[:140],
                "status": (detail.get("status") or "").strip(),
                "link": _VIEW_URL.format(pid=pid),
                "published": dt.isoformat(),
                "updated_raw": upd_raw,
                "first_seen": _now_utc().isoformat(timespec="seconds"),
            }
            added += 1

        _prune(store)
        _save(store)
        if added:
            log.info("techport_store: +%d (total=%d)", added, len(store))
        return added


def load_recent(days: int = 14) -> list[dict]:
    """返回近 days 天的技术港条目（按更新时间倒序）。"""
    store = _load()
    cutoff = _now_utc() - timedelta(days=days)
    out: list[dict] = []
    for v in store.values():
        ref = _parse_updated(v.get("updated_raw") or "") or _parse_updated(v.get("published") or "")
        if ref is None or ref < cutoff:
            continue
        out.append({
            "title": v.get("title") or "",
            "title_en": v.get("title_en") or "",
            "summary": v.get("summary") or "",
            "status": v.get("status") or "",
            "link": v.get("link") or "",
            "published": v.get("published") or "",
        })
    out.sort(key=lambda x: x.get("published") or "", reverse=True)
    return out
