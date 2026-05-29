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
import os  # noqa: E402

log = logging.getLogger(__name__)

app = FastAPI(title="weixin_auto_message")

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
        "endpoints": ["/weixin", "/news/{batch}/{id}", "/ingest/spacenews"],
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


@app.get("/weixin")
def verify_url(msg_signature: str, timestamp: str, nonce: str, echostr: str):
    """企业微信「接收消息」回调 URL 验证。"""
    ret, echo = _crypto.VerifyURL(msg_signature, timestamp, nonce, echostr)
    if ret != 0:
        log.error("VerifyURL failed: ret=%s", ret)
        raise HTTPException(status_code=400, detail=f"VerifyURL ret={ret}")
    return Response(content=echo, media_type="text/plain")


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

    log.info("Received from=%s type=%s content=%r", from_user, msg_type, content[:80])

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
