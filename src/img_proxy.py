"""图片代理：把第三方图片用本机服务器中转后再放进企业微信卡片。

这样：
1. 接收方无论网络环境如何，都能从你已经对外开放的 PUBLIC_BASE_URL 拉到图；
2. 由我们的服务带上正确的 Referer / UA 去抓源图，绕开盗链 / 403；
3. 第一次成功后写入磁盘缓存，重复推送无需再访问源站；
4. 调用方在装卡片前可以用 `prefetch()` 预热——抓不到的图就不要塞 picurl
   （比如 NSF / Cloudflare 全站 403 的源），避免接收方看到一个加载失败的灰框。
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from urllib.parse import urlencode

import requests

from .config import SETTINGS

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
IMG_CACHE_DIR = ROOT / "data" / "img_cache"
IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def public_base() -> str:
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return base
    host = SETTINGS.server_host if SETTINGS.server_host != "0.0.0.0" else "127.0.0.1"
    return f"http://{host}:{SETTINGS.server_port}"


def proxify(image_url: str, referer: str | None = None) -> str:
    """把任意第三方图片 URL 包装成本机 /img?u=... 代理 URL。空输入原样返回。"""
    if not image_url:
        return ""
    if image_url.startswith(public_base()):
        return image_url
    qs = {"u": image_url}
    if referer:
        qs["r"] = referer
    return f"{public_base()}/img?{urlencode(qs)}"


def _cache_key(u: str, r: str) -> str:
    return hashlib.sha256(f"{u}|{r}".encode("utf-8")).hexdigest()[:40]


def cached_bytes(image_url: str, referer: str | None = None) -> bytes | None:
    """读 prefetch 已落盘的图片字节，没缓存返回 None。供发布到公众号时上传用。"""
    if not image_url:
        return None
    key = _cache_key(image_url, referer or "")
    bin_path = IMG_CACHE_DIR / f"{key}.bin"
    if not bin_path.exists() or bin_path.stat().st_size == 0:
        return None
    try:
        return bin_path.read_bytes()
    except Exception:
        return None


def _weserv_url(image_url: str) -> str:
    """把任意源图 URL 转成 images.weserv.nl 公共代理 URL（绕开盗链/Cloudflare）。"""
    from urllib.parse import quote
    stripped = image_url.split("://", 1)[-1]
    return f"https://images.weserv.nl/?url={quote(stripped, safe='')}"


def _try_fetch(url: str, headers: dict, timeout: float):
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        return None, f"err {e}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        return None, f"ct {ct}"
    return resp, ""


def prefetch(image_url: str, referer: str | None = None, *, timeout: float = 12.0) -> bool:
    """主动预热代理缓存。成功（200 + image/*）返回 True，失败 False。

    流程：
    1. 直接抓源图（带 Referer/UA）
    2. 失败时降级用 images.weserv.nl 公共图片代理重试
    """
    if not image_url:
        return False
    r = referer or ""
    key = _cache_key(image_url, r)
    bin_path = IMG_CACHE_DIR / f"{key}.bin"
    ct_path = IMG_CACHE_DIR / f"{key}.ct"
    if bin_path.exists() and ct_path.exists() and bin_path.stat().st_size > 0:
        return True
    headers = {
        "User-Agent": _UA,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if r:
        headers["Referer"] = r

    resp, err = _try_fetch(image_url, headers, timeout)
    if resp is None:
        # 降级：用 weserv.nl 公共图片代理重试（NSF / Cloudflare 之类强盗链通常能拿到）
        log.info("img direct fail %s (%s), retry via weserv", image_url, err)
        ws = _weserv_url(image_url)
        ws_headers = {k: v for k, v in headers.items() if k != "Referer"}
        resp, err = _try_fetch(ws, ws_headers, timeout)
        if resp is None:
            log.info("img weserv fallback also failed %s: %s", image_url, err)
            return False

    data = resp.content
    if not data or len(data) > 20 * 1024 * 1024:
        return False
    ct = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        ct = "image/jpeg"
    bin_path.write_bytes(data)
    ct_path.write_text(ct, encoding="utf-8")
    return True
