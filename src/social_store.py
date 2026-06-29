"""政要社媒（X / Truth Social）独立存储 + LLM 富化。

与每日推送解耦，单独维护一份 data/social_store.json：
- 海外 GH Actions 抓到的原始帖子 POST 到 /ingest/social；
- 服务端对每条**新**帖做 LLM 处理：一律生成中文标题 + 整段翻译并入库（不再按是否航天过滤）；
- 仅当帖子与航天器/太空相关时附带航天视角解读，连同时间/渠道/原文/图片一起入库；
- 小程序后端从这里读取，渲染「政要社媒」栏目。

存储为按 key=``platform:post_id`` 索引的 dict，自带 RETENTION_DAYS 滚动清理（近两周）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import requests

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"\S+")


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


# --------------------------------------------------------------------------
# 社媒配图：服务端自取 + 本地缓存
# --------------------------------------------------------------------------
# 推特/Truth 的图国内服务器直连不到（pbs.twimg.com / nitter / truthsocial 均超时），
# 但公共图片代理 images.weserv.nl 在国内可达、且能回源把这些图取回来。因此这里由
# **服务端经 weserv 下载图片字节 → 落盘到 relay_img → 生成国内可达的 /relay-img URL**，
# 不再依赖海外抓取端的 base64 回传（实测其链路不稳）。取不到就留空（默认不配图）。
_IMG_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_NITTER_PIC_RE = re.compile(r"/pic/(.+)$")
_IMG_MAX_BYTES = 8 * 1024 * 1024


def _source_image_url(url: str, platform: str) -> str:
    """把抓取到的图片地址还原成可被 weserv 回源的真实源地址。

    X 的 nitter ``/pic/media%2F...`` 代理 → 真实 ``pbs.twimg.com/media/...``；
    Truth Social 的 static-assets 地址原样返回。
    """
    if not url:
        return ""
    if platform == "x":
        m = _NITTER_PIC_RE.search(url)
        if m:
            path = unquote(m.group(1)).lstrip("/")
            return "https://pbs.twimg.com/" + path
    return url


def _weserv(url: str) -> str:
    stripped = url.split("://", 1)[-1]
    return f"https://images.weserv.nl/?url={quote(stripped, safe='')}"


def _download_via_weserv(src_url: str) -> tuple[bytes, str] | None:
    """经 images.weserv.nl 下载图片字节，返回 (bytes, mime)。失败返回 None。"""
    if not src_url:
        return None
    try:
        r = requests.get(
            _weserv(src_url),
            headers={"User-Agent": _IMG_UA, "Accept": "image/*,*/*;q=0.8"},
            timeout=25,
        )
    except Exception as e:
        log.info("social image weserv fetch error %s: %s", src_url, e)
        return None
    if r.status_code != 200 or not r.content or len(r.content) > _IMG_MAX_BYTES:
        log.info("social image weserv bad resp %s: HTTP %s size %s",
                 src_url, r.status_code, len(r.content or b""))
        return None
    ct = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        ct = "image/jpeg"
    return r.content, ct


# 注意：实测 truthsocial / pbs.twimg 在国内消费级网络**也加载不出来**（域名被墙），
# 所以中继失败时绝不能回退存原始地址——那样手机只会显示空白/破图。中继不到就留空、不配图。
_PHONE_DIRECT_HOSTS: tuple[str, ...] = ()


def _phone_loadable(url: str) -> bool:
    h = url.split("://", 1)[-1].split("/", 1)[0].lower()
    return any(h == d or h.endswith("." + d) for d in _PHONE_DIRECT_HOSTS)


def _resolve_image(p: dict) -> str:
    """确定一条帖子最终可用的图片 URL。取不到返回 ""。

    取图优先级：
      1) 海外抓取端已回传的字节（image_b64）→ 落盘本地 /relay-img；
      2) 服务端经 weserv 自取源图 → 落盘本地 /relay-img（X 的 twimg 走得通，且国内手机
         直连不到 twimg，必须中继）；
      3) 中继失败兜底：若原图地址是**手机能直连**的源（如 truthsocial CDN），直接返回
         原址，交给手机端 <image> 自行加载（服务器/weserv 取不到，但手机可达）。
    """
    from . import relay_img as _relay

    images = p.get("images") or []
    raw = images[0] if images else ""
    platform = p.get("platform", "")

    # 1) 海外抓取端已回传的字节（若该链路这次恰好成功）
    if p.get("image_b64"):
        try:
            rkey = _relay.store_b64(p["image_b64"], p.get("image_mime", "image/jpeg"))
            if rkey:
                return _relay.url(rkey)
        except Exception:
            log.exception("social image relay(store_b64) failed")

    # 2) 服务端经 weserv 下载源图，落盘本地缓存
    if raw:
        got = _download_via_weserv(_source_image_url(raw, platform))
        if got:
            try:
                rkey = _relay.store_bytes(got[0], got[1])
                if rkey:
                    return _relay.url(rkey)
            except Exception:
                log.exception("social image relay(store_bytes) failed")

    # 3) 中继失败 → 手机可直连的源（truthsocial）直接用原址；其它（nitter/twimg）留空
    if raw and _phone_loadable(raw):
        return raw
    return ""

ROOT = Path(__file__).resolve().parent.parent
STORE_PATH = ROOT / "data" / "social_store.json"
STORE_PATH.parent.mkdir(parents=True, exist_ok=True)

RETENTION_DAYS = 14
# 单次入库最多富化多少条，挡住高频刷屏导致的 LLM 费用失控。
# 可用环境变量 SOCIAL_MAX_PER_INGEST 临时调大（如一次性回填历史）。
MAX_PER_INGEST = int(os.getenv("SOCIAL_MAX_PER_INGEST", "40"))

_LOCK = threading.Lock()

CHANNEL_LABEL = {
    "x": "X（推特）",
    "truth_social": "Truth Social",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _key(post: dict) -> str:
    return f"{post.get('platform','')}:{post.get('post_id','')}"


def _load() -> dict[str, dict]:
    if not STORE_PATH.exists():
        return {}
    try:
        return json.loads(STORE_PATH.read_text("utf-8"))
    except Exception as e:
        log.warning("social_store load failed: %s", e)
        return {}


def _save(store: dict[str, dict]) -> None:
    tmp = STORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE_PATH)


def _prune(store: dict[str, dict]) -> None:
    cutoff = _now() - timedelta(days=RETENTION_DAYS)
    for k in list(store.keys()):
        dt = _parse_iso(store[k].get("published", ""))
        if dt is None or dt < cutoff:
            store.pop(k, None)


def ingest_and_enrich(posts: list[dict]) -> int:
    """对一批原始帖子做去重 + LLM 富化后入库。返回实际新增入库的条数。

    每条只在**首次见到**时调用一次 LLM：一律生成中文标题+翻译并入库，
    解读仅在与航天相关时附带（不再因『不相关』而丢弃）。
    """
    from .summarizer import analyze_social_post

    with _LOCK:
        store = _load()
        seen_keys = set(store.keys())

    # 仅保留库里没有、且本批不重复的，按时间倒序，截断到 MAX_PER_INGEST
    fresh: list[dict] = []
    batch_seen: set[str] = set()
    for p in posts:
        k = _key(p)
        if not p.get("post_id") or k in seen_keys or k in batch_seen:
            continue
        batch_seen.add(k)
        fresh.append(p)
    fresh.sort(key=lambda x: _parse_iso(x.get("published", "")) or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    fresh = fresh[:MAX_PER_INGEST]

    added = 0
    enriched: dict[str, dict] = {}
    for p in fresh:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        # 先确定配图（服务端经 weserv 自取并本地缓存为 /relay-img URL；取不到则空）。
        image_url = _resolve_image(p)
        # 取不到图片 且 正文 <15 词的帖子价值过低，直接丢弃（连 LLM 都不调用以省成本）。
        # 有图的短帖（如配图 + 一句话）仍保留。
        if not image_url and _word_count(text) < 15:
            continue
        res = analyze_social_post(p.get("author_name", ""), text, p.get("platform", ""))
        enriched[_key(p)] = {
            "platform": p.get("platform", ""),
            "channel": CHANNEL_LABEL.get(p.get("platform", ""), p.get("platform", "")),
            "author": p.get("author", ""),
            "author_name": p.get("author_name", ""),
            "post_id": p.get("post_id", ""),
            "url": p.get("url", ""),
            "published": p.get("published", ""),
            "title": res.get("title") or f"{p.get('author_name','')}最新动态",
            "original": text,
            "translation": res.get("translation", ""),
            "analysis": res.get("analysis", ""),
            "image": image_url,
            "first_seen": _now().isoformat(),
        }
        added += 1

    if enriched:
        with _LOCK:
            store = _load()
            store.update(enriched)
            _prune(store)
            _save(store)

    # 回填：对**已入库但配图还不是本地 /relay-img**（旧的失效原址或空）的帖子，
    # 若本批重新带回了可用图（image_b64 / 可下载源），就地把 image 换成本地图；
    # 仍取不到则清成空串，避免手机端继续显示空白/破图。不重复调用 LLM。
    refreshed = _backfill_images(posts)

    log.info("social ingest: %d posts in, %d stored, %d images refreshed",
             len(posts), added, refreshed)
    return added


def _backfill_images(posts: list[dict]) -> int:
    with _LOCK:
        store = _load()
        existing = {k: store[k].get("image", "") or "" for k in store}
    updates: dict[str, str] = {}
    for p in posts:
        k = _key(p)
        if k not in existing:
            continue
        cur = existing[k]
        if "/relay-img/" in cur:
            continue  # 已是本地图，跳过
        new_img = _resolve_image(p)
        if new_img and new_img != cur:
            updates[k] = new_img
        elif cur and not new_img:
            updates[k] = ""  # 原址不可达且这次也没取到 → 清空，别再显示破图
    if not updates:
        return 0
    with _LOCK:
        store = _load()
        for k, v in updates.items():
            if k in store:
                store[k]["image"] = v
        _save(store)
    return len(updates)


def load_recent(days: int = 9) -> list[dict]:
    """返回近 ``days`` 天的政要社媒条目，按发布时间倒序。"""
    cutoff = _now() - timedelta(days=days)
    store = _load()
    out = []
    for v in store.values():
        dt = _parse_iso(v.get("published", ""))
        if dt is None or dt < cutoff:
            continue
        out.append(v)
    out.sort(key=lambda x: _parse_iso(x.get("published", "")) or datetime.min.replace(tzinfo=timezone.utc),
             reverse=True)
    return out


def prune() -> int:
    """供定时清理调用。返回剩余条数。"""
    with _LOCK:
        store = _load()
        before = len(store)
        _prune(store)
        _save(store)
    log.info("social_store prune: %d -> %d", before, len(store))
    return len(store)
