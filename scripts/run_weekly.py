"""手工准备每周合辑，或创建交给企业微信成员确认的群发任务。"""
from __future__ import annotations

import argparse
import json
import logging

from src.weekly_highlights import create_weekly_task, prepare_weekly


def main() -> None:
    parser = argparse.ArgumentParser(description="生成每周航天 Highlights")
    parser.add_argument("--week", help="指定 ISO 周，如 2026-W30")
    parser.add_argument(
        "--create-task",
        action="store_true",
        help="调用企业微信创建待确认群发任务；默认只生成合辑",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使本周已有 msgid 也重新创建任务",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    if args.create_task:
        result = create_weekly_task(args.week, force=args.force)
    else:
        result = prepare_weekly(args.week)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
