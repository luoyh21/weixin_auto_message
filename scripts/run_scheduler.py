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
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import SETTINGS  # noqa: E402
from src.daily import run_daily  # noqa: E402


def make_job(label: str, key: str):
    def _job():
        try:
            run_daily(send=True, session_label=label, session_key=key)
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
    if SETTINGS.evening_hour is not None:
        sched.add_job(
            make_job("晚间", "evening"),
            trigger=CronTrigger(hour=SETTINGS.evening_hour, minute=SETTINGS.evening_minute, timezone=SETTINGS.daily_tz),
            id="evening_brief", misfire_grace_time=600,
        )
        enabled.append(f"晚间 {SETTINGS.evening_hour:02d}:{SETTINGS.evening_minute:02d}")

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
