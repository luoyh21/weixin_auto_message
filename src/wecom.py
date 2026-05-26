"""企业微信应用消息发送（含 access_token 缓存）。"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Iterable

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import SETTINGS

log = logging.getLogger(__name__)

_token_lock = threading.Lock()
_token_cache = {"token": None, "expires_at": 0.0}

API = "https://qyapi.weixin.qq.com/cgi-bin"
# 企业微信单条 text 消息内容上限 2048 字节，markdown 4096 字节
TEXT_LIMIT = 2000
MARKDOWN_LIMIT = 4000


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def _fetch_token() -> tuple[str, float]:
    r = requests.get(
        f"{API}/gettoken",
        params={"corpid": SETTINGS.corp_id, "corpsecret": SETTINGS.secret},
        timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("errcode") != 0:
        raise RuntimeError(f"gettoken failed: {j}")
    expires_at = time.time() + max(60, int(j.get("expires_in", 7200)) - 300)
    return j["access_token"], expires_at


def get_access_token(force: bool = False) -> str:
    with _token_lock:
        if (
            not force
            and _token_cache["token"]
            and _token_cache["expires_at"] > time.time()
        ):
            return _token_cache["token"]
        token, exp = _fetch_token()
        _token_cache["token"] = token
        _token_cache["expires_at"] = exp
        log.info("Refreshed WeCom access_token (valid until %s)", time.ctime(exp))
        return token


def _post_message(payload: dict) -> dict:
    token = get_access_token()
    url = f"{API}/message/send?access_token={token}"
    r = requests.post(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("errcode") == 42001:  # token expired
        token = get_access_token(force=True)
        r = requests.post(
            f"{API}/message/send?access_token={token}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=15,
        )
        j = r.json()
    return j


def _split_text(content: str, limit: int) -> list[str]:
    if len(content.encode("utf-8")) <= limit:
        return [content]
    parts: list[str] = []
    buf: list[str] = []
    cur = 0
    for line in content.splitlines(keepends=True):
        b = len(line.encode("utf-8"))
        if cur + b > limit and buf:
            parts.append("".join(buf))
            buf, cur = [], 0
        buf.append(line)
        cur += b
    if buf:
        parts.append("".join(buf))
    return parts


PART_INTERVAL_SECONDS = 2.0


def _normalize_image_to_jpeg(raw: bytes, *, max_side: int = 1600, quality: int = 85) -> bytes | None:
    """企业微信只接受 jpg/png/gif/bmp，遇到 webp/avif 等不可用格式时本函数
    用 Pillow 解码后统一保存成 JPEG。同时按需缩小过大图片。
    """
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(raw))
        img.load()
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            try:
                bg.paste(img, mask=img.split()[-1])
            except Exception:
                bg.paste(img.convert("RGB"))
            img = bg
        else:
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        out = BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception as e:
        log.warning("PIL normalize failed: %s", e)
        return None


def _origin_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}/"
    except Exception:
        pass
    return ""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def upload_temp_image(image_url: str, *, referer: str | None = None, max_bytes: int = 9 * 1024 * 1024) -> str | None:
    """下载 image_url → 统一转 JPEG → 作为「临时素材」上传企业微信，返回 media_id。

    企业微信限制 image 临时素材 ≤10MB 且只支持 jpg/png/gif/bmp，
    我们统一转 JPEG 保证兼容。referer 可显式传入，用于绕开盗链 403。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    ref = referer or _origin_of(image_url)
    if ref:
        headers["Referer"] = ref
    try:
        r = requests.get(image_url, timeout=15, headers=headers)
        r.raise_for_status()
        raw = r.content
    except Exception as e:
        log.warning("download image failed %s: %s", image_url, e)
        return None

    data = _normalize_image_to_jpeg(raw)
    if data is None:
        # PIL 不可用 / 解码失败时，尝试原样上传（仅当看起来是 jpg/png 时）
        ct = r.headers.get("Content-Type", "").lower()
        if "jpeg" in ct or "jpg" in ct or "png" in ct:
            data = raw
        else:
            return None
    if len(data) > max_bytes:
        log.warning("image too large (%d bytes), skip upload: %s", len(data), image_url)
        return None

    token = get_access_token()
    try:
        resp = requests.post(
            f"{API}/media/upload",
            params={"access_token": token, "type": "image"},
            files={"media": ("hero.jpg", data, "image/jpeg")},
            timeout=30,
        )
        resp.raise_for_status()
        j = resp.json()
        if j.get("errcode") not in (0, None):
            log.warning("upload media failed: %s", j)
            return None
        return j.get("media_id")
    except Exception as e:
        log.warning("upload media error: %s", e)
        return None


def send_image(
    image_url: str | None = None,
    to_user: str | None = None,
    *,
    candidates: list[tuple[str, str | None]] | None = None,
) -> dict | None:
    """便捷封装：上传图片 → 发 image msgtype。

    支持两种调用方式：
    1. 单图：send_image(image_url, ...)
    2. 多候选：send_image(candidates=[(url1, ref1), (url2, ref2), ...])
       依次尝试，第一个上传成功就发出。
    全部失败返回 None。
    """
    tries: list[tuple[str, str | None]] = []
    if candidates:
        tries.extend([(u, r) for (u, r) in candidates if u])
    if image_url:
        tries.append((image_url, None))
    media_id = None
    used_url = None
    for url, ref in tries:
        media_id = upload_temp_image(url, referer=ref)
        if media_id:
            used_url = url
            break
        log.info("send_image: candidate failed, try next: %s", url)
    if not media_id:
        return None
    body = {
        "touser": to_user or SETTINGS.to_user,
        "msgtype": "image",
        "agentid": SETTINGS.agent_id,
        "image": {"media_id": media_id},
        "safe": 0,
    }
    res = _post_message(body)
    log.info("send_image (used %s) -> %s", used_url, res)
    return res


def send_text(content: str, to_user: str | None = None) -> list[dict]:
    """发送纯文本应用消息，超长自动静默分片；分片之间间隔 PART_INTERVAL_SECONDS
    秒再发，确保接收端按序展示。
    """
    receiver = to_user or SETTINGS.to_user
    parts = _split_text(content, TEXT_LIMIT)
    total = len(parts)
    results = []
    for idx, part in enumerate(parts, 1):
        if idx > 1:
            time.sleep(PART_INTERVAL_SECONDS)
        body = {
            "touser": receiver,
            "msgtype": "text",
            "agentid": SETTINGS.agent_id,
            "text": {"content": part},
            "safe": 0,
        }
        results.append(_post_message(body))
        log.info("send_text part %d/%d -> %s", idx, total, results[-1])
    return results


def send_news(
    articles: list[dict],
    to_user: str | None = None,
) -> dict | None:
    """发送图文消息（msgtype=news），单条消息内含多张卡片，首张带大图。

    入参 articles 中每个 dict 字段：
        title:    必填，≤128 字节
        description: 可选，≤512 字节
        url:      点击跳转链接，必填
        picurl:   缩略图绝对 URL；仅首张 article 用大图，其它略
    最多 8 条，超出自动截断。
    """
    if not articles:
        return None
    items = []
    for a in articles[:8]:
        items.append({
            "title": (a.get("title") or "")[:120],
            "description": (a.get("description") or "")[:500],
            "url": a.get("url") or "",
            "picurl": a.get("picurl") or "",
        })
    body = {
        "touser": to_user or SETTINGS.to_user,
        "msgtype": "news",
        "agentid": SETTINGS.agent_id,
        "news": {"articles": items},
        "safe": 0,
    }
    res = _post_message(body)
    log.info("send_news (%d cards) -> %s", len(items), res)
    return res


def send_markdown(content: str, to_user: str | None = None) -> list[dict]:
    """发送 markdown 消息，超长自动分片；分片间隔 PART_INTERVAL_SECONDS 秒保序。"""
    receiver = to_user or SETTINGS.to_user
    parts = _split_text(content, MARKDOWN_LIMIT)
    total = len(parts)
    results = []
    for idx, part in enumerate(parts, 1):
        if idx > 1:
            time.sleep(PART_INTERVAL_SECONDS)
        body = {
            "touser": receiver,
            "msgtype": "markdown",
            "agentid": SETTINGS.agent_id,
            "markdown": {"content": part},
        }
        results.append(_post_message(body))
        log.info("send_markdown part %d/%d -> %s", idx, total, results[-1])
    return results
