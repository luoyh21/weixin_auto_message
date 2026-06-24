"""FastAPI 服务：企业微信回调 + 大模型问答。

挂载路径：GET/POST /weixin
- GET：URL 接入验证 (echostr 回写)
- POST：接收用户文本消息，调用 GPT 回复（上下文为最近一次 daily 缓存）
"""
from __future__ import annotations

import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import hashlib
import requests
from fastapi import FastAPI, Request, Response, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vendor import WXBizMsgCrypt  # noqa: E402

from .config import SETTINGS  # noqa: E402
from .daily import load_latest_cache, _md_links_to_anchor  # noqa: E402
from .summarizer import answer_with_context  # noqa: E402
from .news_pages import page_file, latest_batches  # noqa: E402
from .ingest import save_ingest, INGEST_TOKEN_ENV  # noqa: E402
from .img_proxy import IMG_CACHE_DIR  # noqa: E402
from .dy_pages import page_file as dy_page_file  # noqa: E402
from .join_qr import (  # noqa: E402
    ensure_qrcode, IMG_FILE as JOIN_IMG, current_meta as join_meta,
    ensure_invite_qrcode, INVITE_IMG_FILE,
)
import os  # noqa: E402
import threading  # noqa: E402

log = logging.getLogger(__name__)

app = FastAPI(title="weixin_auto_message")

# ---- 挂载微信小程序后端（独立目录 weixin_miniprogram/backend），统一走本域名 /api ----
try:
    _WORKSPACE = ROOT.parent
    if str(_WORKSPACE) not in sys.path:
        sys.path.insert(0, str(_WORKSPACE))
    from weixin_miniprogram.backend.api import router as _mp_router  # noqa: E402

    app.include_router(_mp_router, prefix="/api")
    log.info("mounted weixin_miniprogram backend at /api")

    # 启动即后台预热新闻缓存，避免第一个用户请求撞上冷构建（曾达 ~13s）。
    try:
        from weixin_miniprogram.backend import news_store as _news_store  # noqa: E402
        threading.Thread(target=_news_store.warm, daemon=True).start()
    except Exception as _we:  # noqa
        logging.getLogger(__name__).warning("news cache warm skip: %s", _we)
except Exception as _e:  # noqa
    logging.getLogger(__name__).warning("mini-program backend not mounted: %s", _e)

_crypto = WXBizMsgCrypt(
    SETTINGS.callback_token,
    SETTINGS.callback_aes_key,
    SETTINGS.corp_id,
)


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "weixin_auto_message",
        "endpoints": ["/weixin", "/news/{batch}/{id}", "/ingest/spacenews", "/ingest/social"],
        "batches": latest_batches(),
    }


_IMG_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@app.get("/img")
def img_proxy(u: str, r: str = ""):
    """图片代理：?u=源图URL&r=Referer。第一次同步抓取源图、落盘缓存、返回；
    后续同 (u,r) 命中缓存直接返回，企业微信卡片里的 picurl 接收方都从这里拉。"""
    if not (u.startswith("http://") or u.startswith("https://")):
        raise HTTPException(status_code=400, detail="bad url")
    key = hashlib.sha256(f"{u}|{r}".encode("utf-8")).hexdigest()[:40]
    bin_path = IMG_CACHE_DIR / f"{key}.bin"
    ct_path = IMG_CACHE_DIR / f"{key}.ct"
    if bin_path.exists() and ct_path.exists():
        return Response(
            content=bin_path.read_bytes(),
            media_type=ct_path.read_text(encoding="utf-8").strip() or "image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    headers = {
        "User-Agent": _IMG_UA,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if r:
        headers["Referer"] = r
    try:
        resp = requests.get(u, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.warning("img_proxy upstream fail u=%s err=%s", u, e)
        raise HTTPException(status_code=502, detail=f"upstream: {e}")
    data = resp.content
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="image too large")
    ct = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    if not ct.startswith("image/"):
        ct = "image/jpeg"
    bin_path.write_bytes(data)
    ct_path.write_text(ct, encoding="utf-8")
    return Response(
        content=data,
        media_type=ct,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/relay-img/{key}")
def relay_img(key: str):
    """直供海外回传、已落盘的图片字节（推特/Truth/盗链图，国内可达）。"""
    from . import relay_img as _relay
    got = _relay.read(key)
    if not got:
        raise HTTPException(status_code=404, detail="not found")
    data, ct = got
    return Response(
        content=data,
        media_type=ct,
        headers={"Cache-Control": "public, max-age=604800"},
    )


_JOIN_PAGE_TPL = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>订阅每日航天速递</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:30px 18px 44px;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;color:#111;background:linear-gradient(180deg,#f4f6fb 0%,#fff 55%);min-height:100vh;display:flex;flex-direction:column;align-items:center}}
.head{{max-width:400px;text-align:center;margin-bottom:18px}}
h1{{font-size:21px;margin:0 0 6px;font-weight:600;letter-spacing:.4px}}
.sub{{font-size:13px;color:#666;margin:0;line-height:1.6}}
.card{{background:#fff;border:1px solid #ececec;border-radius:18px;box-shadow:0 6px 24px rgba(20,30,60,.06);padding:22px 22px 20px;max-width:400px;width:100%;text-align:center;margin-bottom:16px}}
.step{{display:flex;align-items:center;justify-content:center;gap:8px;margin:0 0 4px}}
.num{{width:24px;height:24px;border-radius:50%;background:#3478f6;color:#fff;font-size:14px;font-weight:700;display:inline-flex;align-items:center;justify-content:center}}
.stitle{{font-size:16px;font-weight:600}}
.sdesc{{font-size:12px;color:#777;margin:6px 0 14px;line-height:1.7}}
.qr{{width:240px;height:240px;border:1px solid #eee;border-radius:12px;background:#fafafa;object-fit:contain;padding:8px}}
.warn{{color:#c0392b;font-size:12px;line-height:1.7}}
.foot{{margin-top:6px;font-size:11px;color:#bbb;text-align:center}}
.arrow{{font-size:20px;color:#9aa6b8;margin:-4px 0 12px}}
.inp{{display:block;width:100%;margin:8px 0;padding:12px 14px;font-size:15px;border:1px solid #dfe3ea;border-radius:10px;outline:none}}
.inp:focus{{border-color:#3478f6}}
.btn{{display:block;width:100%;margin-top:10px;padding:13px;font-size:16px;font-weight:600;color:#fff;background:#3478f6;border:none;border-radius:24px;cursor:pointer}}
.btn:disabled{{background:#9fbdf6}}
.msg{{margin:12px 2px 0;font-size:13px;line-height:1.6;min-height:18px}}
.msg.ok{{color:#1a9e57}}
.msg.err{{color:#c0392b}}
.faq{{text-align:left}}
.faq h2{{font-size:15px;font-weight:600;margin:0 0 10px;display:flex;align-items:center;gap:6px}}
.faq .q{{font-size:12.5px;color:#666;margin:0 0 12px;line-height:1.7}}
.faq ol{{margin:0;padding-left:20px}}
.faq li{{font-size:12.5px;color:#444;line-height:1.9;margin-bottom:10px}}
.faq b{{color:#3478f6;font-weight:600}}
</style>
</head><body>
<div class="head">
  <h1>订阅每日航天速递</h1>
  <p class="sub">按以下两步操作，即可在微信里每天收到航天要闻速递</p>
</div>

<div class="card">
  <div class="step"><span class="num">1</span><span class="stitle">加入企业</span></div>
  <p class="sdesc">点下方按钮，微信会弹出「请求获取你的账号」授权，<br>用微信绑定的手机号一键加入「{corp_name}」（成为成员才能收推送）。</p>
  {invite_block}
</div>

<div class="arrow">↓</div>

<div class="card">
  <div class="step"><span class="num">2</span><span class="stitle">关注微信插件</span></div>
  <p class="sdesc">完成第一步后，再扫此码在微信里接收消息<br>（已加入企业，扫码不会再要求验证手机号）</p>
  {plugin_block}
</div>

<div class="card faq">
  <h2>❓ 常见问题</h2>
  <p class="q">如果<b>只能在企业微信中收到消息</b>，而<b>微信里收不到</b>，可按以下步骤排查：</p>
  <ol>
    <li>是否已<b>关注微信插件</b>（即上方第 2 步的二维码）。</li>
    <li>在企业微信「侧边栏 → 设置 → 消息通知 → 仅在企业微信中接收消息」里，<b>关闭</b>两个"仅在企业微信中接收"的开关。</li>
    <li>打开微信中的"航天信息整理机器人"会话，点击右上角的"+"号，选择"设置"，查看"接收企业消息"是否<b>已打开</b>。</li>
  </ol>
</div>

<p class="foot">二维码最近更新：{fetched_at}</p>
</body></html>
"""

_CORP_NAME = "航天新闻播报"


@app.get("/join")
def join_page():
    inv_meta = ensure_invite_qrcode()
    pl_meta = ensure_qrcode() or join_meta()

    if inv_meta and SETTINGS.join_url and INVITE_IMG_FILE.exists():
        inv_ver = (inv_meta.get("fetched_at", "") or "").replace(":", "").replace("-", "")
        invite_block = (
            f'<a class="btn" href="{SETTINGS.join_url}">微信一键加入企业</a>'
            f'<p class="sdesc" style="margin:14px 0 8px">或用微信扫码加入：</p>'
            f'<img class="qr" src="/invite.png?v={inv_ver}" alt="加入企业邀请二维码">'
        )
    else:
        invite_block = (
            '<p class="warn">加入企业邀请链接未配置：请在 .env 设置 '
            'WECOM_JOIN_URL（后台「邀请成员」生成的 work.weixin.qq.com/join/... 链接）。</p>'
        )

    pl_ver = ((pl_meta or {}).get("fetched_at", "") or "").replace(":", "").replace("-", "")
    if pl_meta and JOIN_IMG.exists():
        plugin_block = f'<img class="qr" src="/join.png?v={pl_ver}" alt="微信插件关注二维码">'
    else:
        plugin_block = (
            '<p class="warn">微信插件二维码未配置：请在 .env 设置 '
            'WECOM_WX_PLUGIN_QR（图片路径）或 WECOM_WX_PLUGIN_URL（链接）。</p>'
        )

    fetched_at = (pl_meta or inv_meta or {}).get("fetched_at", "—")
    html = _JOIN_PAGE_TPL.format(
        corp_name=_CORP_NAME, invite_block=invite_block,
        plugin_block=plugin_block, fetched_at=fetched_at,
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/invite.png")
def invite_png():
    ensure_invite_qrcode()
    if not INVITE_IMG_FILE.exists():
        raise HTTPException(status_code=503, detail="加入企业邀请链接未配置（设置 WECOM_JOIN_URL）")
    return FileResponse(INVITE_IMG_FILE, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/join.png")
def join_png():
    ensure_qrcode()
    if not JOIN_IMG.exists():
        raise HTTPException(status_code=503, detail="微信插件二维码未配置，请先在 .env 配置")
    return FileResponse(JOIN_IMG, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/dy/{aweme_id}")
def dy_landing(aweme_id: str):
    p = dy_page_file(aweme_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="douyin landing not found")
    return FileResponse(p, media_type="text/html; charset=utf-8")


@app.get("/news/{batch}/{page_id}")
def news_page(batch: str, page_id: str):
    p = page_file(batch, page_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="page not found")
    return FileResponse(p, media_type="text/html; charset=utf-8")


@app.get("/t/{topic_id}/{page_id}")
def topic_page(topic_id: str, page_id: str):
    """专题情报的中文落地页（持久，不参与 news_pages 轮转）。"""
    try:
        from weixin_miniprogram.backend.topic_intel import topic_page_file
    except Exception:
        raise HTTPException(status_code=404, detail="topic page not available")
    p = topic_page_file(topic_id, page_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="topic page not found")
    return FileResponse(p, media_type="text/html; charset=utf-8")


@app.post("/ingest/spacenews")
async def ingest_spacenews(request: Request, x_auth_token: str | None = Header(default=None)):
    expected = os.getenv(INGEST_TOKEN_ENV, "")
    if not expected:
        raise HTTPException(status_code=503, detail="ingest disabled (no token configured)")
    if x_auth_token != expected:
        raise HTTPException(status_code=401, detail="bad token")
    payload = await request.json()
    items = payload.get("articles") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="expected list of articles or {articles:[...]}")
    path = save_ingest(items)
    return JSONResponse({"ok": True, "saved": str(path), "count": len(items)})


@app.post("/ingest/topic")
async def ingest_topic(request: Request, x_auth_token: str | None = Header(default=None)):
    """专题情报海外抓取入站（复用 SPACENEWS_INGEST_TOKEN）。"""
    expected = os.getenv(INGEST_TOKEN_ENV, "")
    if not expected:
        raise HTTPException(status_code=503, detail="ingest disabled (no token configured)")
    if x_auth_token != expected:
        raise HTTPException(status_code=401, detail="bad token")
    payload = await request.json()
    items = payload.get("articles") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="expected list of articles or {articles:[...]}")
    from .topic_ingest import save_ingest as _save_topic
    path = _save_topic(items)
    return JSONResponse({"ok": True, "saved": str(path), "count": len(items)})


@app.post("/ingest/social")
async def ingest_social(request: Request, x_auth_token: str | None = Header(default=None)):
    """政要社媒海外抓取入站（复用 SPACENEWS_INGEST_TOKEN）。

    收到原始帖子后立即返回，富化（LLM 相关性判定/翻译/解读）放后台线程跑，
    避免 GH Actions 端等待大量 LLM 调用而超时。
    """
    expected = os.getenv(INGEST_TOKEN_ENV, "")
    if not expected:
        raise HTTPException(status_code=503, detail="ingest disabled (no token configured)")
    if x_auth_token != expected:
        raise HTTPException(status_code=401, detail="bad token")
    payload = await request.json()
    items = payload.get("posts") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="expected list of posts or {posts:[...]}")

    def _bg(posts: list):
        try:
            from .social_store import ingest_and_enrich
            ingest_and_enrich(posts)
        except Exception:
            log.exception("social ingest enrich failed")

    threading.Thread(target=_bg, args=(items,), daemon=True).start()
    return JSONResponse({"ok": True, "received": len(items)})


@app.get("/weixin")
def verify_url(msg_signature: str, timestamp: str, nonce: str, echostr: str):
    """企业微信「接收消息」回调 URL 验证。"""
    ret, echo = _crypto.VerifyURL(msg_signature, timestamp, nonce, echostr)
    if ret != 0:
        log.error("VerifyURL failed: ret=%s", ret)
        raise HTTPException(status_code=400, detail=f"VerifyURL ret={ret}")
    return Response(content=echo, media_type="text/plain")


_WELCOME_LOG = ROOT / "data" / "welcome_pushed.json"


def _already_welcomed(user_id: str) -> bool:
    import json as _json
    try:
        data = _json.loads(_WELCOME_LOG.read_text("utf-8")) if _WELCOME_LOG.exists() else {}
    except Exception:
        data = {}
    return user_id in data


def _mark_welcomed(user_id: str):
    import json as _json, time as _time
    try:
        data = _json.loads(_WELCOME_LOG.read_text("utf-8")) if _WELCOME_LOG.exists() else {}
    except Exception:
        data = {}
    data[user_id] = int(_time.time())
    _WELCOME_LOG.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _welcome_push(user_id: str):
    """给新加入的成员补发最近一次半天速递（后台线程执行，避免阻塞回调）。"""
    if not user_id or user_id == SETTINGS.corp_id:
        return
    if _already_welcomed(user_id):
        log.info("welcome skip (already pushed): %s", user_id)
        return
    try:
        from scripts.resend_cache import resend, latest_cache_name
        cache = latest_cache_name()
        if not cache:
            log.warning("welcome push: no cache available for %s", user_id)
            return
        ok, _ = resend(cache, to_user=user_id)
        log.info("welcome push -> %s cache=%s ok=%s", user_id, cache, ok)
        if ok:
            _mark_welcomed(user_id)
    except Exception as e:
        log.exception("welcome push failed for %s: %s", user_id, e)


def _build_reply(to_user: str, from_user: str, content: str) -> str:
    import time
    return (
        f"<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"</xml>"
    )


@app.post("/weixin")
async def receive(request: Request, msg_signature: str, timestamp: str, nonce: str):
    body = (await request.body()).decode("utf-8")
    ret, xml_content = _crypto.DecryptMsg(body, msg_signature, timestamp, nonce)
    if ret != 0:
        log.error("DecryptMsg failed: ret=%s", ret)
        raise HTTPException(status_code=400, detail=f"DecryptMsg ret={ret}")

    root = ET.fromstring(xml_content)
    msg_type = root.findtext("MsgType", "")
    from_user = root.findtext("FromUserName", "")
    to_user = root.findtext("ToUserName", "")
    content = root.findtext("Content", "") or ""
    event = (root.findtext("Event", "") or "").lower()
    change_type = (root.findtext("ChangeType", "") or "").lower()

    log.info("Received from=%s type=%s event=%s content=%r", from_user, msg_type, event, content[:80])

    # 新成员加入：subscribe（关注应用）或通讯录新增成员 → 后台补发最近一次半天速递
    if msg_type == "event":
        if event == "subscribe" or change_type == "create_user":
            new_user = root.findtext("UserID", "") or from_user
            import threading
            threading.Thread(target=_welcome_push, args=(new_user,), daemon=True).start()
        # 所有事件（含 enter_agent / unsubscribe / 心跳）一律空 200，不回复内容
        return Response(content="", media_type="text/plain")

    if msg_type != "text":
        reply = "目前仅支持文本提问，请直接发送文字～"
    else:
        try:
            cache = load_latest_cache() or {}
            articles = (cache.get("spacenews") or []) + (cache.get("opml") or [])
            reply = answer_with_context(content.strip(), articles)
            reply = _md_links_to_anchor(reply)
        except Exception as e:
            log.exception("answer failed: %s", e)
            reply = f"抱歉，回答出现异常：{e}"

    reply_xml = _build_reply(from_user, to_user, reply)
    enc_ret, enc_xml = _crypto.EncryptMsg(reply_xml, nonce, timestamp)
    if enc_ret != 0:
        log.error("EncryptMsg failed: ret=%s", enc_ret)
        raise HTTPException(status_code=500, detail=f"EncryptMsg ret={enc_ret}")
    return Response(content=enc_xml, media_type="application/xml")


def main():
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    uvicorn.run(
        "src.server:app",
        host=SETTINGS.server_host,
        port=SETTINGS.server_port,
        reload=False,
        access_log=True,
    )


if __name__ == "__main__":
    main()
