"""企业微信「接收消息」URL 验证专用最小服务。

⚠️ 仅用于「企业微信后台 → 应用 → 接收消息 → 设置 API 接收 → 点击『保存』」时
   发起的那一次 GET 验证握手。本文件【只】挂载 GET /weixin，故意不接收 POST，
   避免在你还没完成业务开发时把生产消息打到本接口。

握手协议（来源：企业微信官方文档）：
    GET http://<你的域名>/weixin
        ?msg_signature=XXX&timestamp=XXX&nonce=XXX&echostr=ENCRYPT_STR
  - 必须做 URL decode（FastAPI/Starlette 已自动处理）
  - 用 token + timestamp + nonce + echostr 计算 SHA1 校验 msg_signature
  - 用 EncodingAESKey 解密 echostr，取出 msg 字段
  - 1 秒内以纯文本（无引号、无 BOM、无换行）原样返回 msg
本服务完全使用官方 WXBizMsgCrypt 的 VerifyURL() 一步到位完成上面三步。

启动（端口 8503，与 .env 中 SERVER_PORT 一致）：

    cd /root/workspace/weixin_auto_message
    .venv/bin/python -m src.verify_server

确认在企业微信后台保存 URL 成功后，停掉本进程，再启动完整业务服务：

    .venv/bin/python -m src.server   # 这个同时接受 GET 验证 + POST 业务消息

URL 必须使用 80 / 443 / 8000~8999 等企业微信允许的端口。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vendor import WXBizMsgCrypt  # noqa: E402

from .config import SETTINGS  # noqa: E402

log = logging.getLogger(__name__)

app = FastAPI(
    title="weixin_auto_message - URL Verify Only",
    description="仅用于企业微信『接收消息』回调 URL 一次性接入验证。",
)

_crypto = WXBizMsgCrypt(
    SETTINGS.callback_token,
    SETTINGS.callback_aes_key,
    SETTINGS.corp_id,
)


@app.get("/")
def root():
    return {
        "service": "weixin_auto_message verify_server",
        "endpoint": "GET /weixin",
        "purpose": "Only handles WeCom callback URL verification (one-time handshake).",
    }


@app.get("/weixin")
def verify_url(msg_signature: str, timestamp: str, nonce: str, echostr: str):
    """企业微信 URL 接入验证。

    FastAPI 已自动对 query string 做 URL decode，echostr 等参数直接传给
    WXBizMsgCrypt.VerifyURL 即可。验证通过后必须原样回写解密得到的明文 msg，
    response body 不能加引号 / BOM / 换行符。
    """
    ret, echo = _crypto.VerifyURL(msg_signature, timestamp, nonce, echostr)
    if ret != 0:
        log.error("VerifyURL FAILED ret=%s sig=%s ts=%s nonce=%s", ret, msg_signature, timestamp, nonce)
        raise HTTPException(status_code=400, detail=f"VerifyURL ret={ret}")
    log.info("VerifyURL OK -> echo len=%d", len(echo))
    # 必须是 text/plain，原样回写明文
    return Response(content=echo, media_type="text/plain")


def main():
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    log.info(
        "verify_server starting on %s:%d  (token=%s..., aes_key=%s..., corp_id=%s)",
        SETTINGS.server_host,
        SETTINGS.server_port,
        SETTINGS.callback_token[:6],
        SETTINGS.callback_aes_key[:6],
        SETTINGS.corp_id,
    )
    uvicorn.run(
        "src.verify_server:app",
        host=SETTINGS.server_host,
        port=SETTINGS.server_port,
        reload=False,
        access_log=True,
    )


if __name__ == "__main__":
    main()
