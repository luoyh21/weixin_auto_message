"""加载 .env 并暴露强类型配置。"""
from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _req(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"Missing required env: {key}")
    return v


@dataclass(frozen=True)
class Settings:
    # WeCom
    corp_id: str
    agent_id: int
    secret: str
    to_user: str
    callback_token: str
    callback_aes_key: str
    contact_secret: str  # 通讯录同步 Secret，用于 join_qrcode 等通讯录管理接口
    external_secret: str  # 客户联系接口凭据；默认复用已授权自建应用的 Secret
    external_sender: str  # 创建群发任务后负责手动确认的成员 UserId

    # 「微信插件」关注二维码（来自企业微信后台「我的企业 → 微信插件」，静态，无 API）
    wx_plugin_url: str   # 微信插件链接，形如 https://work.weixin.qq.com/...（可本地生成二维码）
    wx_plugin_qr: str    # 后台下载好的二维码图片路径（优先用它，原样展示，最稳）

    # 「加入企业」邀请链接（后台「邀请成员」生成，形如 work.weixin.qq.com/join/...）
    # 走"微信请求获取你的账号"授权流程，用微信绑定手机号一键加入；过期了在 .env 换一行即可。
    join_url: str

    # OpenAI
    openai_api_key: str
    openai_base_url: str
    openai_model: str

    # Server
    server_host: str
    server_port: int

    # Source
    spacenews_rss: str
    opml_path: Path
    nasa_rss: str
    esa_rss: str
    news_max_total: int  # 国际新闻三源(SpaceNews/NASA/ESA)卡片总上限，>8 自动分多条消息

    # Schedule
    morning_hour: int | None
    morning_minute: int
    evening_hour: int | None
    evening_minute: int
    daily_tz: str
    window_hours: int
    weekly_enabled: bool
    weekly_day_of_week: str
    weekly_hour: int
    weekly_minute: int

    cache_dir: Path


def _opt_int(name: str) -> int | None:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return None
    return int(v)


def load() -> Settings:
    cache = ROOT / "data" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    return Settings(
        corp_id=_req("WECOM_CORP_ID"),
        agent_id=int(_req("WECOM_AGENT_ID")),
        secret=_req("WECOM_SECRET"),
        to_user=_req("WECOM_TO_USER"),
        callback_token=_req("WECOM_CALLBACK_TOKEN"),
        callback_aes_key=_req("WECOM_CALLBACK_AES_KEY"),
        contact_secret=os.getenv("WECOM_CONTACT_SECRET", ""),
        # 自建应用被加入「客户联系 API 可调用应用」后，可直接使用应用 Secret。
        # WECOM_EXTERNAL_SECRET 仅保留给使用独立客户联系 Secret 的部署覆盖。
        external_secret=os.getenv("WECOM_EXTERNAL_SECRET", "").strip() or _req("WECOM_SECRET"),
        external_sender=os.getenv("WECOM_EXTERNAL_SENDER", "space_message").strip(),
        wx_plugin_url=os.getenv("WECOM_WX_PLUGIN_URL", "").strip(),
        wx_plugin_qr=os.getenv("WECOM_WX_PLUGIN_QR", "").strip(),
        join_url=os.getenv("WECOM_JOIN_URL", "").strip(),
        openai_api_key=_req("OPENAI_API_KEY"),
        openai_base_url=_req("OPENAI_BASE_URL"),
        openai_model=_req("OPENAI_MODEL"),
        server_host=os.getenv("SERVER_HOST", "0.0.0.0"),
        server_port=int(os.getenv("SERVER_PORT", "8503")),
        spacenews_rss=os.getenv("SPACENEWS_RSS", "https://spacenews.com/feed/"),
        opml_path=ROOT / os.getenv("OPML_PATH", "data/zlzchat.opml"),
        nasa_rss=os.getenv("NASA_RSS", "https://www.nasa.gov/feed/").strip(),
        esa_rss=os.getenv("ESA_RSS", "https://www.esa.int/rssfeed/Our_Activities/Space_News").strip(),
        news_max_total=int(os.getenv("NEWS_MAX_TOTAL", "12")),
        morning_hour=_opt_int("DAILY_MORNING_HOUR"),
        morning_minute=int(os.getenv("DAILY_MORNING_MINUTE", "0")),
        evening_hour=_opt_int("DAILY_EVENING_HOUR"),
        evening_minute=int(os.getenv("DAILY_EVENING_MINUTE", "0")),
        daily_tz=os.getenv("DAILY_TZ", "Asia/Shanghai"),
        window_hours=int(os.getenv("DAILY_WINDOW_HOURS", "12")),
        weekly_enabled=os.getenv("WEEKLY_ENABLED", "1").strip().lower() not in ("0", "false", "no"),
        weekly_day_of_week=os.getenv("WEEKLY_DAY_OF_WEEK", "fri").strip().lower(),
        weekly_hour=int(os.getenv("WEEKLY_HOUR", "9")),
        weekly_minute=int(os.getenv("WEEKLY_MINUTE", "0")),
        cache_dir=cache,
    )


SETTINGS = load()
