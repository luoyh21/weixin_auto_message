"""微信公众号（订阅号 / 服务号）发布封装。

我们只用「草稿箱 + freepublish 发布」这一条链路，**不调用群发接口**，
所以发布后只会出现在公众号文章列表里、产生可分享的永久 mp.weixin.qq.com URL，
而不会主动把推送到关注者会话。

依赖：
- .env: WX_MP_APPID / WX_MP_APPSECRET，且 WX_MP_ENABLED=1
- 服务器出口 IP 已加入公众号"基本配置 → IP 白名单"
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import SETTINGS

log = logging.getLogger(__name__)

API = "https://api.weixin.qq.com"
TOKEN_CACHE = SETTINGS.cache_dir / "wx_mp_token.json"
IMG_INLINE_CACHE = SETTINGS.cache_dir / "wx_mp_uploadimg.json"


@dataclass
class WxMpConfig:
    appid: str
    secret: str
    enabled: bool


def load_config() -> WxMpConfig:
    return WxMpConfig(
        appid=os.getenv("WX_MP_APPID", "").strip(),
        secret=os.getenv("WX_MP_APPSECRET", "").strip(),
        enabled=os.getenv("WX_MP_ENABLED", "0").strip() == "1",
    )


def _now() -> int:
    return int(time.time())


def get_access_token(cfg: WxMpConfig) -> str:
    if not (cfg.appid and cfg.secret):
        raise RuntimeError("WX_MP_APPID/WX_MP_APPSECRET not configured")
    if TOKEN_CACHE.exists():
        try:
            data = json.loads(TOKEN_CACHE.read_text("utf-8"))
            if data.get("appid") == cfg.appid and data.get("expire_at", 0) - 120 > _now():
                return data["access_token"]
        except Exception:
            pass
    r = requests.get(
        f"{API}/cgi-bin/token",
        params={"grant_type": "client_credential", "appid": cfg.appid, "secret": cfg.secret},
        timeout=10,
    )
    j = r.json()
    if "access_token" not in j:
        raise RuntimeError(f"get access_token failed: {j}")
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(
        json.dumps({
            "appid": cfg.appid,
            "access_token": j["access_token"],
            "expire_at": _now() + int(j.get("expires_in", 7200)),
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return j["access_token"]


def _post_json(url: str, payload: dict, *, timeout: float = 30.0) -> dict:
    # 微信对 utf-8 字节流敏感，必须自己 json.dumps，否则中文会变 \uXXXX 触发奇怪问题
    r = requests.post(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=timeout,
    )
    return r.json()


# ---------- 图片上传 ----------

def _load_inline_cache() -> dict:
    if IMG_INLINE_CACHE.exists():
        try:
            return json.loads(IMG_INLINE_CACHE.read_text("utf-8"))
        except Exception:
            pass
    return {}


def _save_inline_cache(d: dict) -> None:
    IMG_INLINE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    IMG_INLINE_CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def _normalize_jpeg(data: bytes) -> bytes:
    """微信对 JPEG 兼容性比较挑（部分上游图片直接报 40113/40137）。
    用 Pillow 重编码成 baseline RGB JPEG，去掉所有 ICC/EXIF/异常段，保证 MP 接收。"""
    try:
        from PIL import Image
        import io as _io
        with Image.open(_io.BytesIO(data)) as im:
            if im.mode != "RGB":
                im = im.convert("RGB")
            buf = _io.BytesIO()
            im.save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue()
    except Exception as e:
        log.warning("normalize jpeg failed, use original bytes: %s", e)
        return data


def upload_image_inline(token: str, data: bytes, *, filename: str = "img.jpg") -> str:
    """上传图文正文用图（uploadimg）。返回 mmbiz.qpic.cn 永久 URL。
    无配额限制，按 sha256 去重。"""
    data = _normalize_jpeg(data)
    key = hashlib.sha256(data).hexdigest()
    cache = _load_inline_cache()
    if key in cache:
        return cache[key]
    r = requests.post(
        f"{API}/cgi-bin/media/uploadimg",
        params={"access_token": token},
        files={"media": (filename, io.BytesIO(data), "image/jpeg")},
        timeout=30,
    )
    j = r.json()
    if "url" not in j:
        raise RuntimeError(f"uploadimg failed: {j}")
    cache[key] = j["url"]
    _save_inline_cache(cache)
    return j["url"]


def add_material_image(token: str, data: bytes, *, filename: str = "thumb.jpg") -> str:
    """添加永久图片素材，返回 media_id（用于 thumb_media_id）。"""
    data = _normalize_jpeg(data)
    r = requests.post(
        f"{API}/cgi-bin/material/add_material",
        params={"access_token": token, "type": "image"},
        files={"media": (filename, io.BytesIO(data), "image/jpeg")},
        timeout=30,
    )
    j = r.json()
    if "media_id" not in j:
        raise RuntimeError(f"add_material failed: {j}")
    return j["media_id"]


def del_material(token: str, media_id: str) -> None:
    try:
        _post_json(
            f"{API}/cgi-bin/material/del_material?access_token={token}",
            {"media_id": media_id},
            timeout=10,
        )
    except Exception:
        pass


# ---------- 草稿 + 发布 ----------

def draft_add(token: str, articles: list[dict]) -> str:
    j = _post_json(
        f"{API}/cgi-bin/draft/add?access_token={token}",
        {"articles": articles},
    )
    if "media_id" not in j:
        raise RuntimeError(f"draft/add failed: {j}")
    return j["media_id"]


def freepublish_submit(token: str, media_id: str) -> str:
    j = _post_json(
        f"{API}/cgi-bin/freepublish/submit?access_token={token}",
        {"media_id": media_id},
    )
    if j.get("errcode", 0) != 0 or "publish_id" not in j:
        raise RuntimeError(f"freepublish/submit failed: {j}")
    return str(j["publish_id"])


def freepublish_get(token: str, publish_id: str) -> dict:
    return _post_json(
        f"{API}/cgi-bin/freepublish/get?access_token={token}",
        {"publish_id": publish_id},
    )


def wait_publish(token: str, publish_id: str, *, timeout: float = 120.0) -> list[str]:
    """轮询发布结果，发布成功（publish_status=0）返回子文章 URL 列表。"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = freepublish_get(token, publish_id)
        status = last.get("publish_status", -1)
        if status == 0:
            urls: list[str] = []
            detail = last.get("article_detail") or {}
            for it in detail.get("item", []) or []:
                u = (it.get("article_url") or "").strip()
                if u:
                    urls.append(u)
            return urls
        if status in (2, 3, 4, 5):
            raise RuntimeError(f"freepublish/get failed status={status} info={last}")
        time.sleep(3)
    raise TimeoutError(f"freepublish/get timeout last={last}")


# ---------- 高层封装 ----------

@dataclass
class PublishResult:
    url: str = ""           # freepublish 拿到的永久 mp.weixin.qq.com URL；无权限时为空
    draft_id: str = ""      # draft/add 返回的 media_id，可用于后台直接定位草稿
    needs_manual: bool = False  # True = 账号没有 freepublish 权限，需要在后台手动点"发表"
    error: str = ""


def publish_aggregate(
    cfg: WxMpConfig,
    *,
    title: str,
    body_html: str,
    digest: str,
    thumb_bytes: bytes,
    author: str = "我们的太空·速递",
    source_url: str = "",
    keep_thumb_on_manual: bool = True,
) -> PublishResult:
    """把一篇聚合图文交付到公众号。

    - 优先 freepublish/submit（永久 URL，不群发关注者）
    - 账号没有发布权限（如个人未认证订阅号，errcode=48001）时退化为「仅写草稿箱」，
      让运营者在公众号后台「草稿箱 → 发表」一键生成 mp 链接
    """
    token = get_access_token(cfg)
    thumb_id = add_material_image(token, thumb_bytes)
    article = {
        "title": title[:60],
        "author": author[:8],
        "digest": (digest or "")[:120],
        "content": body_html,
        "thumb_media_id": thumb_id,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }
    if source_url:
        article["content_source_url"] = source_url

    draft_id = ""
    try:
        draft_id = draft_add(token, [article])
    except Exception as e:
        del_material(token, thumb_id)
        return PublishResult(error=f"draft/add failed: {e}")

    try:
        pub_id = freepublish_submit(token, draft_id)
    except Exception as e:
        msg = str(e)
        if "48001" in msg or "api unauthorized" in msg:
            log.warning("wx_mp account lacks freepublish permission; draft saved only")
            # 草稿要保留以便人工发表；封面也保留（删了草稿就空了）
            return PublishResult(draft_id=draft_id, needs_manual=True)
        del_material(token, thumb_id)
        return PublishResult(error=f"freepublish/submit failed: {e}", draft_id=draft_id)

    try:
        urls = wait_publish(token, pub_id, timeout=180)
        return PublishResult(url=(urls[0] if urls else ""), draft_id=draft_id)
    except Exception as e:
        return PublishResult(error=f"wait_publish failed: {e}", draft_id=draft_id)
    finally:
        # freepublish 成功的话，封面素材已被微信引用进永久文章，可以安全删掉省配额
        if not keep_thumb_on_manual:
            del_material(token, thumb_id)
