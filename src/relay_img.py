"""海外回传图片的本地存储 + 直供。

这台服务器在国内，访问不到推特/Truth Social、且会被 nasaspaceflight 等盗链拦截。
因此这类图片必须由**海外抓取端（GitHub Actions）下载好字节**、随帖子/文章一起回传，
本模块负责把字节落盘、生成一个**国内可达**的 /relay-img/<key> URL，供小程序直接加载。

- 字节按内容 sha256 去重，重复回传不会膨胀；
- 自带 RETENTION_DAYS 滚动清理（近两周，由 cleanup 调用 prune）。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
from pathlib import Path

from .img_proxy import public_base

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = ROOT / "data" / "relay_img"
RELAY_DIR.mkdir(parents=True, exist_ok=True)

RETENTION_DAYS = 14
MAX_BYTES = 8 * 1024 * 1024

_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _norm_mime(mime: str) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    return m if m.startswith("image/") else "image/jpeg"


def store_bytes(data: bytes, mime: str = "image/jpeg") -> str | None:
    """落盘图片字节，返回 key（内容 hash）。失败返回 None。"""
    if not data or len(data) > MAX_BYTES:
        return None
    mime = _norm_mime(mime)
    key = hashlib.sha256(data).hexdigest()[:32]
    bin_path = RELAY_DIR / f"{key}.bin"
    ct_path = RELAY_DIR / f"{key}.ct"
    try:
        if not (bin_path.exists() and bin_path.stat().st_size > 0):
            bin_path.write_bytes(data)
            ct_path.write_text(mime, encoding="utf-8")
        else:
            bin_path.touch()  # 续期，避免被清理
    except Exception as e:
        log.warning("relay_img store failed: %s", e)
        return None
    return key


def store_b64(b64: str, mime: str = "image/jpeg") -> str | None:
    if not b64:
        return None
    try:
        data = base64.b64decode(b64)
    except Exception:
        return None
    return store_bytes(data, mime)


def url(key: str) -> str:
    return f"{public_base()}/relay-img/{key}" if key else ""


def read(key: str) -> tuple[bytes, str] | None:
    """读取已落盘的字节与 content-type，供 /relay-img 路由直供。"""
    if not key or "/" in key or "\\" in key or "." in key:
        return None
    bin_path = RELAY_DIR / f"{key}.bin"
    ct_path = RELAY_DIR / f"{key}.ct"
    if not (bin_path.exists() and bin_path.stat().st_size > 0):
        return None
    try:
        ct = ct_path.read_text(encoding="utf-8").strip() if ct_path.exists() else "image/jpeg"
        return bin_path.read_bytes(), (ct or "image/jpeg")
    except Exception:
        return None


def prune(days: int = RETENTION_DAYS) -> int:
    """删除 mtime 超过 days 的图片。返回剩余 .bin 数量。"""
    cutoff = time.time() - days * 86400
    remain = 0
    for f in RELAY_DIR.glob("*.bin"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                f.with_suffix(".ct").unlink(missing_ok=True)
            else:
                remain += 1
        except Exception:
            pass
    return remain
