"""碎片更新：CelesTrak 近30天新编目的轨道碎片（OBJECT_TYPE=DEB）。

数据源（国内服务器可直连）：
- https://celestrak.org/satcat/records.php?GROUP=last-30-days&FORMAT=json

把「较上次新增」的碎片**全部汇总成当日一条**：无新增当天不出条。
碎片多为编目代码，不强行翻译名称，仅做中文标注。结构对齐 gzh_store：
_load/_save/_prune/load_recent + refresh()，落 data/debris_store.json，保留 14 天。
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
_STORE = _ROOT / "data" / "debris_store.json"
_STATE = _ROOT / "data" / "debris_store.state.json"
_LOCK = threading.Lock()

RETENTION_DAYS = 14
CST = timezone(timedelta(hours=8))
_API = "https://celestrak.org/satcat/records.php?GROUP=last-30-days&FORMAT=json"
_LINK = "https://celestrak.org/satcat/search.php"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 常见拥有方代码 → 中文标注（缺省回退原代码）
_OWNER_ZH = {
    "PRC": "中国", "US": "美国", "CIS": "俄罗斯/独联体", "RU": "俄罗斯",
    "ESA": "欧空局", "FR": "法国", "JPN": "日本", "IND": "印度", "ISRO": "印度",
    "UK": "英国", "GER": "德国", "ITSO": "国际卫星组织", "GLOB": "全球星",
    "ORB": "轨道科学", "SES": "SES", "TBD": "待定",
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _owner_zh(code: str) -> str:
    code = (code or "").strip()
    zh = _OWNER_ZH.get(code)
    return f"{zh}({code})" if zh else (code or "未知")


def _load() -> dict[str, dict]:
    if not _STORE.exists():
        return {}
    try:
        d = json.loads(_STORE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception as e:
        log.warning("debris_store load failed: %s", e)
    return {}


def _save(d: dict[str, dict]) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STORE)


def _load_state() -> dict:
    if not _STATE.exists():
        return {}
    try:
        return json.loads(_STATE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def _prune(store: dict[str, dict]) -> None:
    cutoff = (_now_utc().astimezone(CST) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    for k in [k for k in store if k < cutoff]:
        store.pop(k, None)


def refresh() -> int:
    """抓一次近30天编目，把新增碎片汇总成当日一条。返回新增碎片个数（0=今日无条目）。"""
    try:
        r = requests.get(_API, headers={"User-Agent": _UA, "Accept": "application/json"},
                         timeout=30)
        r.raise_for_status()
        records = r.json() or []
    except Exception as e:
        log.exception("celestrak fetch failed: %s", e)
        return 0

    deb = [x for x in records if (x.get("OBJECT_TYPE") or "").upper() == "DEB"]

    with _LOCK:
        st = _load_state()
        seen = set(str(x) for x in (st.get("seen") or []))
        new = [x for x in deb if str(x.get("NORAD_CAT_ID")) not in seen]
        if not new:
            # 仍把当前 DEB 全集并入 seen，便于后续判增量（首跑会全部计为已见，符合预期）
            if not seen:
                _save_state({"seen": sorted(str(x.get("NORAD_CAT_ID")) for x in deb)})
            log.info("debris_store: no new debris")
            return 0

        new.sort(key=lambda x: (x.get("LAUNCH_DATE") or "", str(x.get("NORAD_CAT_ID"))),
                 reverse=True)
        today_bj = _now_utc().astimezone(CST)
        date_key = today_bj.strftime("%Y-%m-%d")

        lines = []
        for x in new:
            name = (x.get("OBJECT_NAME") or "").strip() or "（无名）"
            oid = (x.get("OBJECT_ID") or "").strip() or "—"
            owner = _owner_zh(x.get("OWNER") or "")
            ld = (x.get("LAUNCH_DATE") or "").strip() or "—"
            lines.append(f"{name}｜国际编号 {oid}｜{owner}｜发射 {ld}")

        store = _load()
        cur = store.get(date_key)
        if cur:
            # 当天多次刷新：合并去重，更新计数与标题
            existing = set(cur.get("norads") or [])
            merged_lines = list(cur.get("lines") or [])
            for x, line in zip(new, lines):
                nid = str(x.get("NORAD_CAT_ID"))
                if nid not in existing:
                    existing.add(nid)
                    merged_lines.append(line)
            n = len(merged_lines)
            cur["norads"] = sorted(existing)
            cur["lines"] = merged_lines
            cur["count"] = n
            cur["title"] = f"碎片更新 · {today_bj.month}月{today_bj.day}日（新增 {n} 个编目碎片）"
            cur["summary"] = f"近30天编目中今日新识别到 {n} 个轨道碎片对象。"
            cur["body"] = "\n".join(merged_lines)
        else:
            n = len(new)
            store[date_key] = {
                "date": date_key,
                "title": f"碎片更新 · {today_bj.month}月{today_bj.day}日（新增 {n} 个编目碎片）",
                "summary": f"近30天编目中今日新识别到 {n} 个轨道碎片对象。",
                "body": "\n".join(lines),
                "lines": lines,
                "count": n,
                "norads": sorted(str(x.get("NORAD_CAT_ID")) for x in new),
                "link": _LINK,
                "published": today_bj.isoformat(timespec="seconds"),
            }

        _prune(store)
        _save(store)
        seen |= set(str(x.get("NORAD_CAT_ID")) for x in new)
        _save_state({"seen": sorted(seen)})
        log.info("debris_store: +%d new debris -> %s", len(new), date_key)
        return len(new)


def load_recent(days: int = 14) -> list[dict]:
    """返回近 days 天的碎片汇总条目（按日期倒序）。"""
    store = _load()
    cutoff = (_now_utc().astimezone(CST) - timedelta(days=days)).strftime("%Y-%m-%d")
    out: list[dict] = []
    for k, v in store.items():
        if k < cutoff:
            continue
        out.append({
            "title": v.get("title") or "",
            "summary": v.get("summary") or "",
            "body": v.get("body") or "",
            "count": v.get("count") or 0,
            "link": v.get("link") or _LINK,
            "published": v.get("published") or "",
        })
    out.sort(key=lambda x: x.get("published") or "", reverse=True)
    return out
