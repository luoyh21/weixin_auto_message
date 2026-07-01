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


def proxify(image_url: str, referer: str | None = None,
            width: int = 0, quality: int = 0) -> str:
    """把任意第三方图片 URL 包装成本机 /img?u=... 代理 URL。空输入原样返回。

    width>0 时附带 &w= 让服务端返回等比缩略图（列表卡片用，省流量），quality 控 JPEG 质量。
    """
    if not image_url:
        return ""
    if image_url.startswith(public_base()):
        return image_url
    qs = {"u": image_url}
    if referer:
        qs["r"] = referer
    if width and width > 0:
        qs["w"] = str(width)
    if quality and quality > 0:
        qs["q"] = str(quality)
    return f"{public_base()}/img?{urlencode(qs)}"


def _cache_key(u: str, r: str, w: int = 0, q: int = 0) -> str:
    # w==0 且 q==0 时保持与历史一致（原图缓存复用）；带缩放参数则独立成变体缓存
    raw = f"{u}|{r}" if not (w or q) else f"{u}|{r}|w{w}q{q}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


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


def _resize(data: bytes, width: int, quality: int) -> tuple[bytes, str] | None:
    """把图片等比缩到最大宽 width、转 JPEG（quality）。失败或无需缩放返回 None。

    仅在原图确实更宽时才缩放，避免把小图放大、也避免无谓重编码。
    """
    if width <= 0:
        return None
    try:
        import io
        from PIL import Image

        im = Image.open(io.BytesIO(data))
        im.load()
        if im.width <= width:
            return None  # 原图不比目标宽，不缩放（保持原字节由调用方处理）
        h = max(1, round(im.height * width / im.width))
        im = im.resize((width, h), Image.LANCZOS)
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            im = im.convert("RGBA")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=quality or 75, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception as e:
        log.warning("img resize failed (w=%s): %s", width, e)
        return None


def get_or_fetch(u: str, r: str = "", width: int = 0, quality: int = 0,
                 *, timeout: float = 12.0) -> tuple[bytes, str] | None:
    """取图片字节：命中缓存直接返回；否则抓源图（直连→weserv 兜底）→ 可选缩略 → 落盘。

    返回 (data, content_type) 或 None（彻底失败）。/img 端点与 prefetch 共用此核心，
    保证「手机按需请求」和「后台预热」用同一套键与同一套缩放策略。
    """
    if not u:
        return None
    key = _cache_key(u, r, width, quality)
    bin_path = IMG_CACHE_DIR / f"{key}.bin"
    ct_path = IMG_CACHE_DIR / f"{key}.ct"
    if bin_path.exists() and ct_path.exists() and bin_path.stat().st_size > 0:
        try:
            return bin_path.read_bytes(), (ct_path.read_text(encoding="utf-8").strip() or "image/jpeg")
        except Exception:
            pass

    headers = {
        "User-Agent": _UA,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if r:
        headers["Referer"] = r

    resp, err = _try_fetch(u, headers, timeout)
    if resp is None:
        # 降级：用 weserv.nl 公共图片代理重试（NSF / Cloudflare 之类强盗链通常能拿到）
        log.info("img direct fail %s (%s), retry via weserv", u, err)
        ws = _weserv_url(u)
        ws_headers = {k: v for k, v in headers.items() if k != "Referer"}
        resp, err = _try_fetch(ws, ws_headers, timeout)
        if resp is None:
            log.info("img weserv fallback also failed %s: %s", u, err)
            return None

    data = resp.content
    if not data or len(data) > 20 * 1024 * 1024:
        return None
    ct = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        ct = "image/jpeg"

    if width and width > 0:
        resized = _resize(data, width, quality)
        if resized is not None:
            data, ct = resized

    try:
        bin_path.write_bytes(data)
        ct_path.write_text(ct, encoding="utf-8")
    except Exception as e:
        log.warning("img cache write failed: %s", e)
    return data, ct


def prefetch(image_url: str, referer: str | None = None, *,
             width: int = 0, quality: int = 0, timeout: float = 12.0) -> bool:
    """主动预热代理缓存。成功（拿到 image/* 字节）返回 True，失败 False。"""
    if not image_url:
        return False
    return get_or_fetch(image_url, referer or "", width, quality, timeout=timeout) is not None
