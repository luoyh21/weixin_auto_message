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

    # Schedule
    morning_hour: int | None
    morning_minute: int
    evening_hour: int | None
    evening_minute: int
    daily_tz: str
    window_hours: int

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
        openai_api_key=_req("OPENAI_API_KEY"),
        openai_base_url=_req("OPENAI_BASE_URL"),
        openai_model=_req("OPENAI_MODEL"),
        server_host=os.getenv("SERVER_HOST", "0.0.0.0"),
        server_port=int(os.getenv("SERVER_PORT", "8503")),
        spacenews_rss=os.getenv("SPACENEWS_RSS", "https://spacenews.com/feed/"),
        opml_path=ROOT / os.getenv("OPML_PATH", "data/zlzchat.opml"),
        morning_hour=_opt_int("DAILY_MORNING_HOUR"),
        morning_minute=int(os.getenv("DAILY_MORNING_MINUTE", "0")),
        evening_hour=_opt_int("DAILY_EVENING_HOUR"),
        evening_minute=int(os.getenv("DAILY_EVENING_MINUTE", "0")),
        daily_tz=os.getenv("DAILY_TZ", "Asia/Shanghai"),
        window_hours=int(os.getenv("DAILY_WINDOW_HOURS", "12")),
        cache_dir=cache,
    )


SETTINGS = load()
