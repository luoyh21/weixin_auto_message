"""企业微信「客户联系」群发任务（由成员在客户端确认后发送）。"""
from __future__ import annotations

import logging
import threading
import time

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import SETTINGS

log = logging.getLogger(__name__)

API = "https://qyapi.weixin.qq.com/cgi-bin"
_token_lock = threading.Lock()
_token_cache = {"token": None, "expires_at": 0.0}
_TOKEN_ERROR_CODES = {40014, 42001}


def _utf8_truncate(value: str, max_bytes: int) -> str:
    raw = (value or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return value or ""
    return raw[:max_bytes].decode("utf-8", errors="ignore")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def _fetch_token() -> tuple[str, float]:
    if not SETTINGS.external_secret:
        raise RuntimeError("未配置可用于客户联系接口的应用 Secret")
    response = requests.get(
        f"{API}/gettoken",
        params={"corpid": SETTINGS.corp_id, "corpsecret": SETTINGS.external_secret},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"客户联系 gettoken 失败: {data}")
    expires_at = time.time() + max(60, int(data.get("expires_in", 7200)) - 300)
    return data["access_token"], expires_at


def get_access_token(force: bool = False) -> str:
    with _token_lock:
        if (
            not force
            and _token_cache["token"]
            and _token_cache["expires_at"] > time.time()
        ):
            return str(_token_cache["token"])
        token, expires_at = _fetch_token()
        _token_cache.update(token=token, expires_at=expires_at)
        return token


def list_external_userids(sender: str | None = None) -> list[str]:
    """获取指定客户联系成员名下的外部联系人 ID，用于显式创建群发任务。"""
    userid = (sender or SETTINGS.external_sender).strip()
    if not userid:
        raise RuntimeError("未配置 WECOM_EXTERNAL_SENDER")
    response = requests.get(
        f"{API}/externalcontact/list",
        params={"access_token": get_access_token(), "userid": userid},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"获取外部联系人列表失败: {data}")
    return list(dict.fromkeys(data.get("external_userid") or []))


def _post(path: str, payload: dict) -> dict:
    token = get_access_token()
    response = requests.post(
        f"{API}/{path}",
        params={"access_token": token},
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errcode") in _TOKEN_ERROR_CODES:
        response = requests.post(
            f"{API}/{path}",
            params={"access_token": get_access_token(force=True)},
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企业微信 {path} 失败: {data}")
    return data


def create_attachment_mass_task(
    *,
    attachments: list[dict],
    text: str = "",
    sender: str | None = None,
    external_userids: list[str] | None = None,
) -> dict:
    """创建客户群发任务；当前企业微信链路需带非空 text 才能稳定分发附件。"""
    task_sender = (sender or SETTINGS.external_sender).strip()
    if not task_sender:
        raise RuntimeError("未配置 WECOM_EXTERNAL_SENDER")
    if not attachments:
        raise ValueError("群发任务至少需要一个附件")
    if len(attachments) > 9:
        raise ValueError("企业微信群发任务最多支持 9 个附件")

    normalized: list[dict] = []
    for attachment in attachments:
        msgtype = attachment.get("msgtype")
        if msgtype == "link":
            source = attachment.get("link") or {}
            url = str(source.get("url") or "")
            picurl = str(source.get("picurl") or "")
            if not url.startswith(("https://", "http://")):
                raise ValueError("链接卡片 url 必须是绝对 HTTP(S) 地址")
            if picurl and not picurl.startswith(("https://", "http://")):
                raise ValueError("链接卡片 picurl 必须是绝对 HTTP(S) 地址")
            link = {
                "title": _utf8_truncate(str(source.get("title") or ""), 128),
                "desc": _utf8_truncate(str(source.get("desc") or ""), 512),
                "url": _utf8_truncate(url, 2048),
            }
            if picurl:
                link["picurl"] = _utf8_truncate(picurl, 2048)
            normalized.append({"msgtype": "link", "link": link})
        elif msgtype == "image":
            source = attachment.get("image") or {}
            pic_url = str(source.get("pic_url") or "")
            media_id = str(source.get("media_id") or "")
            if not media_id and not pic_url.startswith(("https://", "http://")):
                raise ValueError("图片附件需要 media_id 或绝对 HTTP(S) pic_url")
            image: dict = {}
            if media_id:
                image["media_id"] = media_id
            else:
                image["pic_url"] = _utf8_truncate(pic_url, 2048)
            normalized.append({"msgtype": "image", "image": image})
        else:
            raise ValueError(f"暂不支持的群发附件类型: {msgtype}")

    payload: dict = {
        "chat_type": "single",
        "sender": task_sender,
        "allow_select": False,
        "attachments": normalized,
    }
    if text:
        payload["text"] = {"content": _utf8_truncate(text, 4000)}
    if external_userids:
        payload["external_userid"] = list(dict.fromkeys(external_userids))[:10000]

    result = _post("externalcontact/add_msg_template", payload)
    log.info(
        "created external mass task sender=%s msgid=%s attachments=%d customers=%s",
        task_sender,
        result.get("msgid"),
        len(normalized),
        len(external_userids) if external_userids else "all",
    )
    return result


def cancel_mass_task(msgid: str) -> dict:
    """停止尚未由成员确认发送的企业群发任务；已发送消息无法撤回。"""
    if not msgid or not msgid.strip():
        raise ValueError("msgid 不能为空")
    result = _post("externalcontact/cancel_groupmsg_send", {"msgid": msgid.strip()})
    log.info("cancelled external mass task msgid=%s", msgid)
    return result


def create_link_mass_task(
    *,
    title: str,
    description: str,
    url: str,
    picurl: str = "",
    text: str = "",
    sender: str | None = None,
    external_userids: list[str] | None = None,
) -> dict:
    """创建单聊客户群发任务；消息仍需指定成员在企业微信中手动确认。

    未传 external_userids 时，企业微信会选择 sender 名下的全部可群发客户。
    """
    attachments = [{
        "msgtype": "link",
        "link": {"title": title, "desc": description, "url": url, "picurl": picurl},
    }]
    if not text:
        return create_attachment_mass_task(
            attachments=attachments,
            sender=sender,
            external_userids=external_userids,
        )

    task_sender = (sender or SETTINGS.external_sender).strip()
    payload: dict = {
        "chat_type": "single",
        "sender": task_sender,
        "allow_select": False,
        "attachments": attachments,
    }
    if text:
        payload["text"] = {"content": _utf8_truncate(text, 4000)}
    if external_userids:
        payload["external_userid"] = list(dict.fromkeys(external_userids))[:10000]

    result = _post("externalcontact/add_msg_template", payload)
    log.info(
        "created external mass task sender=%s msgid=%s customers=%s",
        task_sender,
        result.get("msgid"),
        len(external_userids) if external_userids else "all",
    )
    return result
