"""微信公众平台（公众号）服务器配置 URL 验证最小服务。

⚠️ 与企业微信的 src/verify_server.py 不是一回事：
   - 企业微信「接收消息」用 msg_signature + WXBizMsgCrypt（安全模式才解密 echostr）；
   - 公众号「服务器配置」首次提交时，无论明文/兼容/安全模式，握手都走【明文签名】：
       GET <你的URL>?signature=XXX&timestamp=XXX&nonce=XXX&echostr=XXX
     校验方式：
       1) 将 token、timestamp、nonce 三个参数做【字典序】排序；
       2) 拼接成一个字符串后做 sha1，得到本地签名；
       3) 与请求里的 signature 比对，相等即来自微信服务器（合法）；
       4) 原样返回 echostr（纯文本，无引号/BOM/换行）。

公众号后台填的：
    URL          = http://8.130.209.181        （无端口 => 80 端口、根路径 /）
    Token        = heting
    EncodingAESKey = 9gsLt5TGDUQTyYs0SVt4jDvHOQhhQjeJcWsWskqsnKs（安全模式 POST 收发消息时才用到，
                     本握手不需要）
    消息加解密方式 = 安全模式

启动（监听 0.0.0.0:80，需 root）：
    cd /root/workspace/weixin_auto_message
    sudo .venv/bin/python -m src.mp_verify_server
"""
from __future__ import annotations

import hashlib
import logging
import os

from fastapi import FastAPI, Response

# 公众号后台「服务器配置」里的 Token（可用环境变量 MP_CALLBACK_TOKEN 覆盖）
MP_TOKEN = os.getenv("MP_CALLBACK_TOKEN", "heting")
# 监听端口：公众号 URL 不带端口即 80；http 仅支持 80，https 仅支持 443
MP_PORT = int(os.getenv("MP_VERIFY_PORT", "80"))
MP_HOST = os.getenv("MP_VERIFY_HOST", "0.0.0.0")

log = logging.getLogger(__name__)

app = FastAPI(title="weixin_auto_message - 公众号 URL 验证", description="仅处理公众号服务器配置 URL 接入验证。")


def _check_signature(signature: str, timestamp: str, nonce: str) -> bool:
    """字典序排序 token/timestamp/nonce -> 拼接 -> sha1 -> 比对 signature。"""
    arr = sorted([MP_TOKEN, timestamp, nonce])
    sha1 = hashlib.sha1("".join(arr).encode("utf-8")).hexdigest()
    return sha1 == signature


def _verify(signature: str, timestamp: str, nonce: str, echostr: str) -> Response:
    ok = _check_signature(signature, timestamp, nonce)
    if not ok:
        local = hashlib.sha1("".join(sorted([MP_TOKEN, timestamp, nonce])).encode()).hexdigest()
        log.error("signature MISMATCH: got=%s local=%s ts=%s nonce=%s", signature, local, timestamp, nonce)
        return Response(content="signature mismatch", media_type="text/plain", status_code=403)
    log.info("signature OK -> echo back echostr len=%d", len(echostr))
    # 原样回写 echostr，纯文本，无引号/BOM/换行
    return Response(content=echostr, media_type="text/plain")


@app.get("/")
def verify_root(signature: str = "", timestamp: str = "", nonce: str = "", echostr: str = ""):
    if echostr:
        return _verify(signature, timestamp, nonce, echostr)
    return Response(content="ok", media_type="text/plain")


# 兜底：万一公众号 URL 带了路径（比如 /wx），同样处理验证
@app.get("/{path:path}")
def verify_any(path: str, signature: str = "", timestamp: str = "", nonce: str = "", echostr: str = ""):
    if echostr:
        return _verify(signature, timestamp, nonce, echostr)
    return Response(content="ok", media_type="text/plain")


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    log.info("公众号 URL 验证服务启动 %s:%d  token=%s", MP_HOST, MP_PORT, MP_TOKEN)
    uvicorn.run("src.mp_verify_server:app", host=MP_HOST, port=MP_PORT, reload=False, access_log=True)


if __name__ == "__main__":
    main()
