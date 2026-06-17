"""关注引导用到的两种二维码：

1) 企业码（加入企业）—— 调 API：cgi-bin/corp/get_join_qrcode（需通讯录同步 Secret 换 token）。
   外部用户扫这个码"加入企业"，成为成员（落到应用可见范围）后才能收到应用推送。
   注意：能否"扫码直接加入/是否需要验证"由企业微信后台「成员加入方式」决定，API 不能绕过。

2) 微信插件码 —— 静态，来自后台「我的企业 → 微信插件」，没有 API。
   仅供"已在通讯录里的成员"在微信里收发消息。手机号不在通讯录会被要求验证身份，
   所以必须先完成第 1 步（加入企业）后再扫它，才不会要求验证。

引导页 /join 把两步串起来展示：第一步加入企业，第二步关注微信插件。

落盘：
    data/join/join_qr.png        微信插件码（对外 /join.png）
    data/join/join_qr.json
    data/join/enterprise_qr.png  企业码（对外 /enterprise.png）
    data/join/enterprise_qr.json
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from src.config import SETTINGS

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
JOIN_DIR = ROOT / "data" / "join"
JOIN_DIR.mkdir(parents=True, exist_ok=True)

# 微信插件码（静态）
META_FILE = JOIN_DIR / "join_qr.json"
IMG_FILE = JOIN_DIR / "join_qr.png"

# 企业码（API get_join_qrcode）
ENT_META_FILE = JOIN_DIR / "enterprise_qr.json"
ENT_IMG_FILE = JOIN_DIR / "enterprise_qr.png"

# 加入企业邀请链接（WECOM_JOIN_URL）本地生成的二维码
INVITE_META_FILE = JOIN_DIR / "invite_qr.json"
INVITE_IMG_FILE = JOIN_DIR / "invite_qr.png"

_TOKEN_CACHE = ROOT / "data" / "cache" / "wecom_contact_token.json"


# ─────────────────────────── 通用：通讯录同步 token ───────────────────────────
def _get_contact_access_token(force: bool = False) -> str:
    """通讯录同步 Secret 对应的 access_token，带本地缓存。"""
    secret = SETTINGS.contact_secret
    if not secret:
        raise RuntimeError("WECOM_CONTACT_SECRET 未配置，无法调用 get_join_qrcode")
    now = time.time()
    if not force and _TOKEN_CACHE.exists():
        try:
            cache = json.loads(_TOKEN_CACHE.read_text("utf-8"))
            if cache.get("expire_at", 0) - 300 > now:
                return cache["access_token"]
        except Exception:
            pass
    url = (
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        f"?corpid={SETTINGS.corp_id}&corpsecret={secret}"
    )
    r = requests.get(url, timeout=10).json()
    if r.get("errcode", 0) != 0:
        raise RuntimeError(f"通讯录 access_token 获取失败: {r}")
    tok = r["access_token"]
    expires_in = int(r.get("expires_in", 7200))
    _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_CACHE.write_text(
        json.dumps({"access_token": tok, "expire_at": now + expires_in}),
        encoding="utf-8",
    )
    log.info("Refreshed WeCom contact access_token (valid for %ds)", expires_in)
    return tok


def _read_meta(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return None


# ─────────────────────────── 1) 企业码（加入企业，API）───────────────────────────
def fetch_enterprise_qrcode(size_type: int = 2) -> dict:
    """调 get_join_qrcode 拿"加入企业"二维码，下载落盘，返回 meta。"""
    token = _get_contact_access_token()

    def _call(t: str) -> dict:
        return requests.get(
            "https://qyapi.weixin.qq.com/cgi-bin/corp/get_join_qrcode"
            f"?access_token={t}&size_type={size_type}",
            timeout=15,
        ).json()

    res = _call(token)
    if res.get("errcode", 0) != 0 and res.get("errcode") in (40014, 42001, 41001):
        token = _get_contact_access_token(force=True)
        res = _call(token)
    if res.get("errcode", 0) != 0:
        raise RuntimeError(f"get_join_qrcode 失败: {res}")

    qrcode_url = res["join_qrcode"]
    img = requests.get(qrcode_url, timeout=20)
    img.raise_for_status()
    ENT_IMG_FILE.write_bytes(img.content)

    now = datetime.now(timezone.utc).astimezone()
    meta = {
        "qrcode_url": qrcode_url,
        "size_type": size_type,
        "fetched_at": now.isoformat(timespec="seconds"),
        "expire_at": (now + timedelta(days=7)).isoformat(timespec="seconds"),
        "source": "cgi-bin/corp/get_join_qrcode",
        "bytes": len(img.content),
    }
    ENT_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("enterprise(join) QR refreshed: %d bytes, expire ~ %s", len(img.content), meta["expire_at"])
    return meta


def current_enterprise_meta() -> dict | None:
    if not ENT_META_FILE.exists() or not ENT_IMG_FILE.exists():
        return None
    return _read_meta(ENT_META_FILE)


def ensure_enterprise_qrcode() -> dict | None:
    """企业码有效期 7 天，剩余 <12h 或缺失则刷新；失败保留旧码。"""
    meta = current_enterprise_meta()
    need = True
    if meta:
        try:
            exp = datetime.fromisoformat(meta["expire_at"])
            if exp - datetime.now(exp.tzinfo) > timedelta(hours=12):
                need = False
        except Exception:
            pass
    if need:
        try:
            meta = fetch_enterprise_qrcode()
        except Exception as e:
            log.exception("ensure_enterprise_qrcode failed, keep stale: %s", e)
    return meta


# ───────────────── 1b) 加入企业邀请链接二维码（WECOM_JOIN_URL）─────────────────
def ensure_invite_qrcode() -> dict | None:
    """按 WECOM_JOIN_URL 本地生成"加入企业"邀请二维码；URL 变化自动重建。"""
    url = SETTINGS.join_url
    if not url:
        # 配置清空则清理旧文件
        for f in (INVITE_IMG_FILE, INVITE_META_FILE):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
        return None
    meta = _read_meta(INVITE_META_FILE) if INVITE_META_FILE.exists() else None
    if meta and meta.get("url") == url and INVITE_IMG_FILE.exists():
        return meta
    try:
        import qrcode
    except Exception as e:
        log.warning("qrcode lib missing: %s", e)
        return None
    qr = qrcode.QRCode(border=2, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(INVITE_IMG_FILE)
    now = datetime.now(timezone.utc).astimezone()
    meta = {"url": url, "fetched_at": now.isoformat(timespec="seconds"), "bytes": INVITE_IMG_FILE.stat().st_size}
    INVITE_META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("invite QR (from WECOM_JOIN_URL) rebuilt: %d bytes", meta["bytes"])
    return meta


# ─────────────────────────── 2) 微信插件码（静态）───────────────────────────
def _plugin_config_sig() -> str | None:
    qr_path = SETTINGS.wx_plugin_qr
    if qr_path:
        p = Path(qr_path)
        if not p.is_absolute():
            p = ROOT / qr_path
        if p.exists():
            return f"img:{p}:{int(p.stat().st_mtime)}"
        log.warning("WECOM_WX_PLUGIN_QR 指向的文件不存在: %s", p)
    if SETTINGS.wx_plugin_url:
        return f"url:{SETTINGS.wx_plugin_url}"
    return None


def _resolve_plugin_qr_path() -> Path | None:
    qr_path = SETTINGS.wx_plugin_qr
    if not qr_path:
        return None
    p = Path(qr_path)
    if not p.is_absolute():
        p = ROOT / qr_path
    return p if p.exists() else None


def _generate_qr_from_url(url: str) -> None:
    try:
        import qrcode
    except Exception as e:  # pragma: no cover
        raise RuntimeError("需要 qrcode 库：pip install 'qrcode[pil]'") from e
    qr = qrcode.QRCode(border=2, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(IMG_FILE)


def fetch_plugin_qrcode() -> dict:
    sig = _plugin_config_sig()
    if sig is None:
        raise RuntimeError(
            "未配置微信插件二维码：请在 .env 设置 WECOM_WX_PLUGIN_QR（图片路径）或 WECOM_WX_PLUGIN_URL（链接）"
        )
    qr_src = _resolve_plugin_qr_path()
    if qr_src is not None:
        shutil.copyfile(qr_src, IMG_FILE)
        source = "static-image"
    else:
        _generate_qr_from_url(SETTINGS.wx_plugin_url)
        source = "qr-from-url"
    now = datetime.now(timezone.utc).astimezone()
    meta = {
        "source": source,
        "plugin_url": SETTINGS.wx_plugin_url,
        "fetched_at": now.isoformat(timespec="seconds"),
        "config_sig": sig,
        "bytes": IMG_FILE.stat().st_size,
    }
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("wx-plugin QR rebuilt: source=%s, %d bytes", source, meta["bytes"])
    return meta


def current_meta() -> dict | None:
    if not META_FILE.exists() or not IMG_FILE.exists():
        return None
    return _read_meta(META_FILE)


def _purge_plugin_stale() -> None:
    for f in (IMG_FILE, META_FILE):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass


def ensure_qrcode() -> dict | None:
    """微信插件码：与当前配置一致则复用，否则重建；未配置则清理旧文件返回 None。"""
    sig = _plugin_config_sig()
    if sig is None:
        if current_meta() is not None or IMG_FILE.exists():
            _purge_plugin_stale()
        return None
    meta = current_meta()
    if meta and meta.get("config_sig") == sig and IMG_FILE.exists():
        return meta
    try:
        return fetch_plugin_qrcode()
    except Exception as e:
        log.exception("ensure_qrcode(plugin) failed: %s", e)
        return current_meta()


# ─────────────────────────── 调度入口 ───────────────────────────
def fetch_join_qrcode(*args, **kwargs) -> dict:
    """供 run_scheduler 调用：刷新两种码。返回企业码 meta（向后兼容）。"""
    ent = None
    try:
        ent = ensure_enterprise_qrcode()
    except Exception as e:
        log.warning("refresh enterprise QR failed: %s", e)
    try:
        ensure_qrcode()
    except Exception as e:
        log.warning("refresh plugin QR failed: %s", e)
    return ent or {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    print("enterprise:", json.dumps(ensure_enterprise_qrcode(), ensure_ascii=False))
    print("plugin:", json.dumps(ensure_qrcode(), ensure_ascii=False))
