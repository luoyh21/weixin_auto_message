"""单次运行：抓取 + 总结 + 推送企业微信，并打印结果。

用法（在项目根目录）：
    .venv/bin/python -m scripts.run_once            # 抓取、总结并真实发送
    .venv/bin/python -m scripts.run_once --no-send  # 只抓取与总结，不发送
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.daily import run_daily  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-send", action="store_true", help="只生成总结，不推送")
    parser.add_argument("--session", choices=["morning", "evening", "daily"], default="daily", help="缓存前缀")
    parser.add_argument("--label", default=None, help="标题里的中文标签，默认按 session 推断")
    parser.add_argument("--hours", type=int, default=None, help="抓取过去 N 小时；缺省读 .env DAILY_WINDOW_HOURS")
    args = parser.parse_args()

    label_map = {"morning": "早间", "evening": "晚间", "daily": "每日"}
    label = args.label or label_map[args.session]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    rec = run_daily(send=not args.no_send, session_label=label, session_key=args.session, hours=args.hours)
    print("\n========== 总结 ==========")
    print(rec["summary"])
    print("==========================")
    print(f"SpaceNews: {len(rec['spacenews'])} 条；OPML: {len(rec['opml'])} 条；sent={rec['sent']}")


if __name__ == "__main__":
    main()
