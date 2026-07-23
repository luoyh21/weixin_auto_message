"""每日定时调度：早间速递（默认 08:00）+ 晚间速递（默认 17:00），均 Asia/Shanghai。

用法：
    .venv/bin/python -m scripts.run_scheduler

时间通过 .env 中：
    DAILY_MORNING_HOUR / DAILY_MORNING_MINUTE
    DAILY_EVENING_HOUR / DAILY_EVENING_MINUTE
    DAILY_TZ
任一 *_HOUR 留空即关闭该班次。
抓取窗口由 DAILY_WINDOW_HOURS 控制（默认 12 小时）。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

ZLZCHAT_BASE = "http://8.130.209.181:10082"
ZLZCHAT_KEY = "lq3525926"
# 需刷新的公众号 wxsId（与 OPML 里两个订阅源对应，格式须带 MP_WXS_ 前缀）
ZLZCHAT_WXS_IDS = ["MP_WXS_3931671274", "MP_WXS_3094327014", "MP_WXS_3927397899"]


def update_zlzchat_feeds():
    """早间推送前调用 zlzchat /updateFeed 接口，逐个 wxsId 刷新公众号 RSS。"""
    for wxs_id in ZLZCHAT_WXS_IDS:
        url = f"{ZLZCHAT_BASE}/updateFeed?key={ZLZCHAT_KEY}&wxsId={wxs_id}"
        try:
            r = requests.get(url, timeout=60)
            logging.info("zlzchat updateFeed wxsId=%s -> %s %s", wxs_id, r.status_code, r.text[:200])
        except Exception as e:
            logging.exception("zlzchat updateFeed wxsId=%s failed: %s", wxs_id, e)


def sync_gzh_store():
    """每隔几小时刷新一次公众号 RSS 并入独立库，保证小程序能看到每条更新。

    与每日推送解耦：先 updateFeed 让 zlzchat 拉取新文章，再抓 OPML 并入库。
    """
    update_zlzchat_feeds()
    try:
        from src.gzh_store import refresh as _gzh_refresh  # noqa: E402
        n = _gzh_refresh(hours=72)
        logging.info("gzh_store sync: +%d new", n)
    except Exception as e:
        logging.exception("gzh_store sync failed: %s", e)


def sync_space_feeds():
    """每日刷新三新栏目：技术港(TechPort) / 每日发射(LL2) / 碎片更新(CelesTrak)。

    三者各自 try 包裹、互不影响；失败只记日志，不阻断其余。
    """
    try:
        from src.techport_store import refresh as _tp_refresh  # noqa: E402
        logging.info("techport_store sync: +%d", _tp_refresh())
    except Exception as e:
        logging.exception("techport_store sync failed: %s", e)
    try:
        from src.launch_store import refresh as _ls_refresh  # noqa: E402
        logging.info("launch_store sync: +%d", _ls_refresh())
    except Exception as e:
        logging.exception("launch_store sync failed: %s", e)
    try:
        from src.launch_store import refresh_upcoming as _lu_refresh  # noqa: E402
        logging.info("launch_upcoming sync: %d", _lu_refresh())
    except Exception as e:
        logging.exception("launch_upcoming sync failed: %s", e)
    try:
        from src.debris_store import refresh as _ds_refresh  # noqa: E402
        logging.info("debris_store sync: +%d", _ds_refresh())
    except Exception as e:
        logging.exception("debris_store sync failed: %s", e)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os  # noqa: E402

from src.config import SETTINGS  # noqa: E402
from src.daily import run_daily  # noqa: E402
from src.join_qr import fetch_join_qrcode  # noqa: E402
from src.cleanup import run as run_cleanup  # noqa: E402
from src.wecom import send_text  # noqa: E402
from src.weekly_highlights import create_weekly_task  # noqa: E402

# 每 6 天提醒谁更换二维码、提示要覆盖的文件
QR_REMIND_USER = "LuoYiHe"
QR_PATH_HINT = "data/join/plugin_qr.png"


def remind_replace_qrcode():
    """每 6 天给 LuoYiHe 个人推一条"更换微信插件二维码"的提醒。"""
    base = (os.getenv("PUBLIC_BASE_URL", "") or "").rstrip("/")
    msg = (
        "🔔 微信插件二维码更换提醒（每 6 天）\n\n"
        "请到「企业微信管理后台 → 我的企业 → 微信插件」下载最新二维码，\n"
        f"覆盖服务器文件：{QR_PATH_HINT}（覆盖后自动生效，无需重启）。"
    )
    if base:
        msg += f"\n\n当前展示页：{base}/join"
    try:
        send_text(msg, to_user=QR_REMIND_USER)
        logging.info("qr replace reminder sent to %s", QR_REMIND_USER)
    except Exception as e:
        logging.exception("qr replace reminder failed: %s", e)


def daily_cleanup():
    try:
        run_cleanup(days=14)
    except Exception as e:
        logging.exception("cleanup failed: %s", e)


def weekly_highlights():
    """打包近 7 天内容，并给指定客户联系成员创建待确认群发任务。"""
    try:
        task = create_weekly_task()
        logging.info("weekly highlights task ready: %s", task.get("msgid"))
    except Exception as e:
        logging.exception("weekly highlights failed: %s", e)


def douyin_selfcheck():
    """每日推送前轻量自检抖音接口；仅在异常时给 LuoYiHe 推一条提醒。"""
    try:
        from src.douyin import selfcheck
        ok, detail = selfcheck()
    except Exception as e:
        ok, detail = False, f"自检执行异常：{e}"
    logging.info("douyin selfcheck ok=%s detail=%s", ok, detail)
    if not ok:
        msg = (
            "⚠️ 抖音自检异常\n\n"
            f"{detail}\n\n"
            "可能原因：抖音 API 容器 Cookie 失效 / 容器异常。\n"
            "处理：检查 douyin_api 容器并更新其登录 Cookie（sec_user_id 无需改动）。"
        )
        try:
            send_text(msg, to_user="LuoYiHe")
        except Exception as e:
            logging.exception("douyin selfcheck alert failed: %s", e)


def refresh_join_qrcode():
    """每日刷新两种码：企业码（get_join_qrcode，7天有效）+ 微信插件码（静态）。"""
    try:
        fetch_join_qrcode()
        logging.info("join QRs (enterprise + plugin) synced")
    except Exception as e:
        logging.warning("join QR sync skipped: %s", e)


def _session_hours(key: str) -> int:
    """根据早/晚两次定时点之差，算出当前 session 的覆盖窗口（小时）。

    - morning：覆盖 *上一次晚间任务 ~ 当前早间任务*
    - evening：覆盖 *当天早间任务 ~ 当前晚间任务*
    - 仅启用一档：退化为 24h（避免漏播）
    - 任一时间点缺失：退化到 DAILY_WINDOW_HOURS
    """
    M, E = SETTINGS.morning_hour, SETTINGS.evening_hour
    if M is None and E is None:
        return SETTINGS.window_hours
    if M is None or E is None:
        return 24
    if key == "morning":
        h = (M - E) % 24 or 24
    elif key == "evening":
        h = (E - M) % 24 or 24
    else:
        h = SETTINGS.window_hours
    return h


def make_job(label: str, key: str):
    def _job():
        hours = _session_hours(key)
        logging.info("%s session start, window=%dh", label, hours)
        try:
            run_daily(send=True, session_label=label, session_key=key, hours=hours)
        except Exception as e:
            logging.exception("%s job error: %s", label, e)
    return _job


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    sched = BlockingScheduler(timezone=SETTINGS.daily_tz)

    enabled = []
    if SETTINGS.morning_hour is not None:
        sched.add_job(
            make_job("早间", "morning"),
            trigger=CronTrigger(hour=SETTINGS.morning_hour, minute=SETTINGS.morning_minute, timezone=SETTINGS.daily_tz),
            id="morning_brief", misfire_grace_time=600,
        )
        enabled.append(f"早间 {SETTINGS.morning_hour:02d}:{SETTINGS.morning_minute:02d}")
        # 早间推送前 22 分钟，刷新 zlzchat 公众号订阅
        pre = (datetime(2000, 1, 1, SETTINGS.morning_hour, SETTINGS.morning_minute)
               - timedelta(minutes=22))
        sched.add_job(
            update_zlzchat_feeds,
            trigger=CronTrigger(hour=pre.hour, minute=pre.minute, timezone=SETTINGS.daily_tz),
            id="zlzchat_feed_refresh", misfire_grace_time=300,
        )
        enabled.append(f"公众号刷新 {pre.hour:02d}:{pre.minute:02d}")
        # 早间推送前 25 分钟，轻量自检抖音接口（仅异常时提醒 LuoYiHe）
        pre_dy = (datetime(2000, 1, 1, SETTINGS.morning_hour, SETTINGS.morning_minute)
                  - timedelta(minutes=25))
        sched.add_job(
            douyin_selfcheck,
            trigger=CronTrigger(hour=pre_dy.hour, minute=pre_dy.minute, timezone=SETTINGS.daily_tz),
            id="douyin_selfcheck", misfire_grace_time=300,
        )
        enabled.append(f"抖音自检 {pre_dy.hour:02d}:{pre_dy.minute:02d}")
    if SETTINGS.evening_hour is not None:
        sched.add_job(
            make_job("晚间", "evening"),
            trigger=CronTrigger(hour=SETTINGS.evening_hour, minute=SETTINGS.evening_minute, timezone=SETTINGS.daily_tz),
            id="evening_brief", misfire_grace_time=600,
        )
        enabled.append(f"晚间 {SETTINGS.evening_hour:02d}:{SETTINGS.evening_minute:02d}")

    # 每 6 小时刷新一次公众号 RSS 并入独立库（与推送解耦），保证小程序不漏每条更新
    _gzh_start = datetime.now().replace(minute=10, second=0, microsecond=0)
    sched.add_job(
        sync_gzh_store,
        trigger=IntervalTrigger(hours=6, start_date=_gzh_start, timezone=SETTINGS.daily_tz),
        id="gzh_store_sync", misfire_grace_time=1800,
    )
    enabled.append("公众号库同步 每6小时")

    # 每天 04:10 刷新三新栏目（技术港 / 每日发射 / 碎片更新），各自直连抓取一次
    sched.add_job(
        sync_space_feeds,
        trigger=CronTrigger(hour=4, minute=10, timezone=SETTINGS.daily_tz),
        id="space_feeds_sync", misfire_grace_time=3600,
    )
    enabled.append("三新栏目同步 每日04:10")

    # 每天 03:30 把「微信插件」二维码按 .env 配置同步一次（静态来源，便宜）
    sched.add_job(
        refresh_join_qrcode,
        trigger=CronTrigger(hour=3, minute=30, timezone=SETTINGS.daily_tz),
        id="join_qrcode_refresh", misfire_grace_time=3600,
    )
    enabled.append("微信插件二维码 每日03:30")

    # 每 6 天 09:30 提醒 LuoYiHe 更换微信插件二维码（真正的 6 天间隔，不受月份边界影响）
    _qr_start = datetime.now().replace(hour=9, minute=30, second=0, microsecond=0)
    sched.add_job(
        remind_replace_qrcode,
        trigger=IntervalTrigger(days=6, start_date=_qr_start, timezone=SETTINGS.daily_tz),
        id="qr_replace_remind", misfire_grace_time=3600,
    )
    enabled.append("二维码更换提醒 每6天09:30→LuoYiHe")

    # 每天 03:45 清理：ingest / cache / translate_cache / img_cache / dy_pages / news_pages，仅保留 14 天（约两周）
    sched.add_job(
        daily_cleanup,
        trigger=CronTrigger(hour=3, minute=45, timezone=SETTINGS.daily_tz),
        id="daily_cleanup", misfire_grace_time=3600,
    )
    enabled.append("缓存清理 每日03:45")

    # 每周打包 highlights 并创建群发任务；真正发送仍由 space_message 在客户端确认。
    if SETTINGS.weekly_enabled and SETTINGS.external_secret and SETTINGS.external_sender:
        sched.add_job(
            weekly_highlights,
            trigger=CronTrigger(
                day_of_week=SETTINGS.weekly_day_of_week,
                hour=SETTINGS.weekly_hour,
                minute=SETTINGS.weekly_minute,
                timezone=SETTINGS.daily_tz,
            ),
            id="weekly_highlights",
            misfire_grace_time=3600,
        )
        enabled.append(
            f"每周合辑 {SETTINGS.weekly_day_of_week} "
            f"{SETTINGS.weekly_hour:02d}:{SETTINGS.weekly_minute:02d}"
            f"→{SETTINGS.external_sender}"
        )
    elif SETTINGS.weekly_enabled:
        logging.warning(
            "weekly highlights disabled: configure WECOM_EXTERNAL_SENDER"
        )

    if not enabled:
        logging.error("Both DAILY_MORNING_HOUR and DAILY_EVENING_HOUR are empty; nothing to schedule.")
        return

    logging.info(
        "Scheduler started: tz=%s, window=%dh; %s",
        SETTINGS.daily_tz, SETTINGS.window_hours, ", ".join(enabled),
    )
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()


if __name__ == "__main__":
    main()
