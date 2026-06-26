"""每日发射：The Space Devs Launch Library 2 当日发射库。

数据源（国内服务器可直连）：
- https://ll.thespacedevs.com/2.2.0/launch/?net__gte=<>&net__lte=<>&ordering=net
  覆盖「当天（北京时间）NET」的发射（含已发射与即将发射）。

每个发射入库一条，火箭/任务名经 LLM 译中文。结构对齐 gzh_store：
_load/_save/_prune/load_recent + refresh()，落 data/launch_store.json，保留 14 天。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_STORE = _ROOT / "data" / "launch_store.json"
_LOCK = threading.Lock()

RETENTION_DAYS = 14
CST = timezone(timedelta(hours=8))
_API = "https://ll.thespacedevs.com/2.2.0/launch/"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _net_bj(net_iso: str) -> str:
    d = _parse_iso(net_iso)
    return d.astimezone(CST).strftime("%m-%d %H:%M") if d else ""


def _load() -> dict[str, dict]:
    if not _STORE.exists():
        return {}
    try:
        d = json.loads(_STORE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception as e:
        log.warning("launch_store load failed: %s", e)
    return {}


def _save(d: dict[str, dict]) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STORE)


def _prune(store: dict[str, dict]) -> None:
    cutoff = _now_utc() - timedelta(days=RETENTION_DAYS)
    for k in [k for k, v in store.items()
              if (_parse_iso(v.get("published") or "") or _now_utc()) < cutoff]:
        store.pop(k, None)


def _today_bj_window_utc() -> tuple[str, str]:
    """当天（北京）00:00~23:59:59 换算成 UTC 的 ISO（带 Z）。"""
    now_bj = _now_utc().astimezone(CST)
    start_bj = now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
    end_bj = now_bj.replace(hour=23, minute=59, second=59, microsecond=0)
    fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return fmt(start_bj), fmt(end_bj)


def _webcast(result: dict) -> str:
    vids = result.get("vidURLs") or []
    for v in vids:
        u = (v.get("url") if isinstance(v, dict) else v) or ""
        if u:
            return u
    return ""


def refresh() -> int:
    """抓一次当天发射并入库（新发射译中文）。返回新增条数。"""
    from .summarizer import translate_zh

    gte, lte = _today_bj_window_utc()
    try:
        r = requests.get(_API, params={
            "net__gte": gte, "net__lte": lte, "ordering": "net", "limit": 40,
            "mode": "detailed",
        }, headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        results = r.json().get("results") or []
    except Exception as e:
        log.exception("launch list fetch failed: %s", e)
        return 0

    with _LOCK:
        store = _load()
        added = 0
        for res in results:
            lid = res.get("id")
            if not lid:
                continue
            name_en = (res.get("name") or "").strip()
            net = res.get("net") or ""
            provider = ((res.get("launch_service_provider") or {}).get("name") or "").strip()
            pad = (res.get("pad") or {})
            pad_name = (pad.get("name") or "").strip()
            location = ((pad.get("location") or {}).get("name") or "").strip()
            status = ((res.get("status") or {}).get("name") or "").strip()
            mission = (res.get("mission") or {})
            mission_desc = (mission.get("description") or "").strip()
            image = res.get("image") or ""
            link = _webcast(res)

            cur = store.get(lid)
            if cur is None:
                zh = translate_zh(name_en, mission_desc)
                store[lid] = {
                    "launch_id": lid,
                    "title": zh.get("title") or name_en,
                    "name_en": name_en,
                    "summary": zh.get("summary") or mission_desc[:140],
                    "provider": provider,
                    "pad": pad_name,
                    "location": location,
                    "status": status,
                    "net": net,
                    "net_bj": _net_bj(net),
                    "image": image,
                    "link": link,
                    "published": net or _now_utc().isoformat(),
                    "first_seen": _now_utc().isoformat(timespec="seconds"),
                }
                added += 1
            else:
                # 已存在：只刷新会变的状态/时间/图，避免重复调用 LLM
                cur["status"] = status or cur.get("status", "")
                cur["net"] = net or cur.get("net", "")
                cur["net_bj"] = _net_bj(net) or cur.get("net_bj", "")
                if net:
                    cur["published"] = net
                if image and not cur.get("image"):
                    cur["image"] = image
                if link and not cur.get("link"):
                    cur["link"] = link

        _prune(store)
        _save(store)
        if added:
            log.info("launch_store: +%d (total=%d)", added, len(store))
        return added


def load_recent(days: int = 14) -> list[dict]:
    """返回近 days 天的发射条目（按 NET 倒序）。"""
    store = _load()
    cutoff = _now_utc() - timedelta(days=days)
    out: list[dict] = []
    for v in store.values():
        ref = _parse_iso(v.get("published") or "")
        if ref is None or ref < cutoff:
            continue
        out.append({
            "title": v.get("title") or "",
            "name_en": v.get("name_en") or "",
            "summary": v.get("summary") or "",
            "provider": v.get("provider") or "",
            "pad": v.get("pad") or "",
            "location": v.get("location") or "",
            "status": v.get("status") or "",
            "net_bj": v.get("net_bj") or "",
            "image": v.get("image") or "",
            "link": v.get("link") or "",
            "published": v.get("published") or "",
        })
    out.sort(key=lambda x: x.get("published") or "", reverse=True)
    return out
