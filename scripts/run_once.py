"""单次运行：抓取 + 总结 + 推送企业微信，并打印结果。

用法（在项目根目录）：
    .venv/bin/python -m scripts.run_once            # 仅发给 LuoYiHe（默认）
    .venv/bin/python -m scripts.run_once --all      # 发给 .env 里配置的全员（@all）
    .venv/bin/python -m scripts.run_once --no-send  # 只抓取与总结，不发送

只有定时任务（scripts.run_scheduler）会按 .env 的 WECOM_TO_USER 发送给全员；
所有手工触发的 run_once 默认走 LuoYiHe，避免临时测试打扰其他成员。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-send", action="store_true", help="只生成总结，不推送")
    parser.add_argument("--session", choices=["morning", "evening", "daily"], default="daily", help="缓存前缀")
    parser.add_argument("--label", default=None, help="标题里的中文标签，默认按 session 推断")
    parser.add_argument("--hours", type=int, default=None, help="抓取过去 N 小时；缺省读 .env DAILY_WINDOW_HOURS")
    parser.add_argument("--all", action="store_true", help="发给 .env 配置的全部接收者；默认只发 LuoYiHe")
    parser.add_argument("--to", default=None, help="覆盖收件人 UserId（管道分隔），如 LuoYiHe|WenYueJie")
    args = parser.parse_args()

    # ===== 非定时手工运行：默认只发 LuoYiHe =====
    if args.to:
        os.environ["WECOM_TO_USER"] = args.to
    elif not args.all:
        os.environ["WECOM_TO_USER"] = "LuoYiHe"
    # 必须在覆盖 env 之后再 import，否则 SETTINGS 已读旧值
    from src.daily import run_daily  # noqa: E402

    label_map = {"morning": "早间", "evening": "晚间", "daily": "每日"}
    label = args.label or label_map[args.session]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.info("run_once recipient = %s", os.environ.get("WECOM_TO_USER", "(env default)"))
    rec = run_daily(send=not args.no_send, session_label=label, session_key=args.session, hours=args.hours)
    print("\n========== 总结 ==========")
    print(rec["summary"])
    print("==========================")
    print(f"SpaceNews: {len(rec['spacenews'])} 条；OPML: {len(rec['opml'])} 条；sent={rec['sent']}")


if __name__ == "__main__":
    main()
