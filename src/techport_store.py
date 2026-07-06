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

# NASA 2020 技术分类（TX 一级）代码 -> 中文
_TX_ZH: dict[str, str] = {
    "TX01": "推进系统",
    "TX02": "飞行计算与航电",
    "TX03": "航天电源与储能",
    "TX04": "机器人系统",
    "TX05": "通信、导航与轨道碎片跟踪",
    "TX06": "人体健康、生命保障与居住系统",
    "TX07": "探索目的地系统",
    "TX08": "传感器与科学仪器",
    "TX09": "进入、下降与着陆(EDL)",
    "TX10": "自主系统",
    "TX11": "软件、建模仿真与信息处理",
    "TX12": "材料、结构、机械与制造",
    "TX13": "地面、测试与地表系统",
    "TX14": "热管理系统",
    "TX15": "飞行器系统",
    "TX16": "空管与靶场跟踪系统",
    "TX17": "制导、导航与控制(GN&C)",
}

# 项目状态英文 -> 中文
_STATUS_ZH: dict[str, str] = {
    "Active": "进行中",
    "Completed": "已完成",
    "Canceled": "已取消",
    "Cancelled": "已取消",
    "Not Started": "未开始",
    "On Hold": "已暂停",
}

_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _fmt_date(date_str: str, year, month) -> str:
    """把 TechPort 的起止时间规整成「YYYY年M月」；缺失则退回原串。"""
    try:
        y = int(year) if year else 0
        m = int(month) if month else 0
        if y and m:
            return f"{y}年{m}月"
        if y:
            return f"{y}年"
    except Exception:
        pass
    s = (date_str or "").strip()
    # 兜底解析 "Mar 2026"
    parts = s.split()
    if len(parts) == 2 and parts[0][:3].lower() in _MONTH_NUM:
        return f"{parts[1]}年{_MONTH_NUM[parts[0][:3].lower()]}月"
    return s


def _tx_category(detail: dict) -> tuple[str, str]:
    """返回 (技术类别中文, 类别代码)。取 primaryTx（一级取根）。"""
    tx = detail.get("primaryTx") or {}
    code = (tx.get("code") or "").strip()
    root = code[:4] if code else ""
    zh = _TX_ZH.get(root, "")
    if not zh and tx.get("title"):
        zh = tx.get("title")  # 未收录的代码退回英文标题
    return zh, code


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
            status_en = (detail.get("status") or "").strip()
            cat_zh, cat_code = _tx_category(detail)
            store[str(pid)] = {
                "project_id": pid,
                "title": zh.get("title") or title_en,
                "title_en": title_en,
                "summary": zh.get("summary") or desc_en[:140],
                "status": status_en,
                "status_zh": _STATUS_ZH.get(status_en, status_en),
                "start_date": _fmt_date(detail.get("startDateString"), detail.get("startYear"), detail.get("startMonth")),
                "end_date": _fmt_date(detail.get("endDateString"), detail.get("endYear"), detail.get("endMonth")),
                "trl_begin": detail.get("trlBegin"),
                "trl_end": detail.get("trlEnd"),
                "category": cat_zh,
                "category_code": cat_code,
                "program": ((detail.get("program") or {}).get("acronym")
                            or (detail.get("program") or {}).get("title") or "").strip(),
                "link": _VIEW_URL.format(pid=pid),
                "published": dt.isoformat(),
                "updated_raw": upd_raw,
                "first_seen": _now_utc().isoformat(timespec="seconds"),
            }
            added += 1

        # 回填：历史条目缺结构化字段（起止/TRL/类别/计划）时补抓一次详情（不再调 LLM），每轮封顶。
        backfilled = 0
        for key, v in store.items():
            if backfilled >= 15:
                break
            if "trl_begin" in v and "category" in v:
                continue
            detail = _fetch_detail(v.get("project_id"))
            if not detail:
                continue
            status_en = (detail.get("status") or v.get("status") or "").strip()
            cat_zh, cat_code = _tx_category(detail)
            v["status"] = status_en
            v["status_zh"] = _STATUS_ZH.get(status_en, status_en)
            v["start_date"] = _fmt_date(detail.get("startDateString"), detail.get("startYear"), detail.get("startMonth"))
            v["end_date"] = _fmt_date(detail.get("endDateString"), detail.get("endYear"), detail.get("endMonth"))
            v["trl_begin"] = detail.get("trlBegin")
            v["trl_end"] = detail.get("trlEnd")
            v["category"] = cat_zh
            v["category_code"] = cat_code
            v["program"] = ((detail.get("program") or {}).get("acronym")
                            or (detail.get("program") or {}).get("title") or "").strip()
            backfilled += 1

        _prune(store)
        _save(store)
        if added or backfilled:
            log.info("techport_store: +%d, backfill %d (total=%d)", added, backfilled, len(store))
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
            "status_zh": v.get("status_zh") or v.get("status") or "",
            "start_date": v.get("start_date") or "",
            "end_date": v.get("end_date") or "",
            "trl_begin": v.get("trl_begin"),
            "trl_end": v.get("trl_end"),
            "category": v.get("category") or "",
            "category_code": v.get("category_code") or "",
            "program": v.get("program") or "",
            "link": v.get("link") or "",
            "published": v.get("published") or "",
        })
    out.sort(key=lambda x: x.get("published") or "", reverse=True)
    return out
