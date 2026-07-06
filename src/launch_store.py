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

from . import space_i18n

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_STORE = _ROOT / "data" / "launch_store.json"
_UPCOMING_STORE = _ROOT / "data" / "launch_upcoming.json"
_LOCK = threading.Lock()
_UP_LOCK = threading.Lock()

RETENTION_DAYS = 14
UPCOMING_DAYS = 30   # 未来发射看板覆盖未来 30 天
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


def _sln_url(slug: str) -> str:
    """The Space Devs 面向用户的发射页（Space Launch Now）。"""
    return f"https://spacelaunchnow.me/launch/{slug}/" if slug else ""


def _best_link(res: dict) -> str:
    """原文链接优先用 Space Devs 自家发射页（转发），否则回退直播/资料链接。"""
    slug = (res.get("slug") or "").strip()
    if slug:
        return _sln_url(slug)
    wc = _webcast(res)
    if wc:
        return wc
    for u in (res.get("infoURLs") or []):
        uu = (u.get("url") if isinstance(u, dict) else u) or ""
        if uu:
            return uu
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
            link = _best_link(res)

            cur = store.get(lid)
            if cur is None:
                zh = translate_zh(name_en, mission_desc)
                store[lid] = {
                    "launch_id": lid,
                    "title": zh.get("title") or name_en,
                    "name_en": name_en,
                    "summary": zh.get("summary") or mission_desc[:140],
                    "provider": provider,
                    "provider_zh": space_i18n.provider_zh(provider),
                    "pad": pad_name,
                    "location": location,
                    "location_zh": space_i18n.place_zh(location or pad_name),
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
                # 已存在：只刷新会变的状态/时间/图/链接，避免重复调用 LLM；
                # 顺便补齐历史条目缺失的中文字段与 Space Devs 链接。
                cur["status"] = status or cur.get("status", "")
                cur["net"] = net or cur.get("net", "")
                cur["net_bj"] = _net_bj(net) or cur.get("net_bj", "")
                if net:
                    cur["published"] = net
                if image and not cur.get("image"):
                    cur["image"] = image
                if link:
                    cur["link"] = link
                if not cur.get("provider_zh") and (provider or cur.get("provider")):
                    cur["provider_zh"] = space_i18n.provider_zh(provider or cur.get("provider", ""))
                if not cur.get("location_zh"):
                    cur["location_zh"] = space_i18n.place_zh(
                        location or cur.get("location") or pad_name or cur.get("pad", ""))

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
            "provider_zh": v.get("provider_zh") or space_i18n.provider_zh(v.get("provider") or ""),
            "pad": v.get("pad") or "",
            "location": v.get("location") or "",
            "location_zh": v.get("location_zh") or space_i18n.place_zh(v.get("location") or v.get("pad") or ""),
            "status": v.get("status") or "",
            "net_bj": v.get("net_bj") or "",
            "image": v.get("image") or "",
            "link": v.get("link") or "",
            "published": v.get("published") or "",
        })
    out.sort(key=lambda x: x.get("published") or "", reverse=True)
    return out


# ---------------------------------------------------------------------------
# 未来发射看板：未来 UPCOMING_DAYS 天内的发射计划（聚合成一条看板）。
# 不逐条译中文（数量多、成本高）：火箭/任务名保留英文，提供方/发射场走静态中文映射。
# ---------------------------------------------------------------------------
def _net_bj_full(net_iso: str) -> str:
    d = _parse_iso(net_iso)
    return d.astimezone(CST).strftime("%m-%d %H:%M") if d else ""


def refresh_upcoming() -> int:
    """抓未来 30 天发射计划并落 data/launch_upcoming.json（列表）。返回条数。"""
    now = _now_utc()
    gte = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lte = (now + timedelta(days=UPCOMING_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = requests.get(_API, params={
            "net__gte": gte, "net__lte": lte, "ordering": "net", "limit": 60,
            "mode": "detailed",
        }, headers={"User-Agent": _UA, "Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        results = r.json().get("results") or []
    except Exception as e:
        log.exception("upcoming launch fetch failed: %s", e)
        return 0

    def _name(v) -> str:
        """兼容 list/detailed 两种模式：字段可能是 {name:..} 或直接字符串。"""
        if isinstance(v, dict):
            return (v.get("name") or "").strip()
        return (v or "").strip() if isinstance(v, str) else ""

    items: list[dict] = []
    for res in results:
        net = res.get("net") or ""
        provider = _name(res.get("launch_service_provider"))
        pad = res.get("pad") if isinstance(res.get("pad"), dict) else {}
        loc_raw = pad.get("location") if isinstance(pad, dict) else None
        location = _name(loc_raw)
        status = _name(res.get("status"))
        items.append({
            "name_en": (res.get("name") or "").strip(),
            "provider": provider,
            "provider_zh": space_i18n.provider_zh(provider),
            "location": location,
            "location_zh": space_i18n.place_zh(location or _name(pad)),
            "status": status,
            "net": net,
            "net_bj": _net_bj_full(net),
            "link": _best_link(res),
        })

    payload = {
        "generated_at": _now_utc().isoformat(timespec="seconds"),
        "days": UPCOMING_DAYS,
        "items": items,
    }
    with _UP_LOCK:
        _UPCOMING_STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _UPCOMING_STORE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_UPCOMING_STORE)
    log.info("launch_upcoming: %d launches in next %dd", len(items), UPCOMING_DAYS)
    return len(items)


def load_upcoming() -> dict:
    """返回未来发射看板数据 {generated_at, days, items:[...]}（无则空）。"""
    if not _UPCOMING_STORE.exists():
        return {"generated_at": "", "days": UPCOMING_DAYS, "items": []}
    try:
        d = json.loads(_UPCOMING_STORE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception as e:
        log.warning("launch_upcoming load failed: %s", e)
    return {"generated_at": "", "days": UPCOMING_DAYS, "items": []}
