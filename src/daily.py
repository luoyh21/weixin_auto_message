"""每日任务：抓取 -> 总结 -> 缓存 -> 推送企业微信。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .config import SETTINGS
from .spacenews import fetch_recent as fetch_spacenews
from .opml_feeds import fetch_opml_recent
from .summarizer import daily_summary
import os
import re

from .wecom import send_text, send_image
from .news_pages import prepare_news_pages

log = logging.getLogger(__name__)


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _md_links_to_anchor(text: str) -> str:
    """把 markdown `[text](url)` 转成企业微信文本支持的 `<a href="url">text</a>`。

    顺便兜底：把残留的孤立裸 URL 用 <a> 包一层，避免在客户端只展示纯文本。
    """
    text = _MD_LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    return text


def _wrap_header(body: str, date_str: str, session_label: str = "每日") -> str:
    return f"【🚀 航天{session_label}速递 {date_str}】\n{body.strip()}"


def _split_overview_and_list(summary: str) -> tuple[str, str]:
    """把整段 summary 切成『头部(标题+总览)』+『新闻列表』两块。

    新闻列表的起点是第一行以编号或 emoji 板块标识开头的内容（『🌍』『📰』『1.』）。
    """
    lines = summary.splitlines()
    cut = None
    for i, line in enumerate(lines):
        s = line.lstrip()
        if s.startswith(("🌍", "📰")) or re.match(r"^\d+\.\s", s):
            cut = i
            break
    if cut is None:
        return summary, ""
    head = "\n".join(lines[:cut]).rstrip()
    tail = "\n".join(lines[cut:]).rstrip()
    return head, tail


def cache_path(date_str: str | None = None, session: str = "daily") -> Path:
    return SETTINGS.cache_dir / f"{session}_{date_str or _today_str()}.json"


def load_latest_cache() -> dict | None:
    """返回最近一份缓存（用于 /weixin 回复时的上下文，按修改时间取最新）。"""
    files = sorted(SETTINGS.cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("load_latest_cache failed: %s", e)
        return None


def _public_base() -> str:
    """中文翻译页对外的 base URL，默认按服务器 host+port 拼。"""
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return base
    return f"http://{SETTINGS.server_host if SETTINGS.server_host != '0.0.0.0' else '127.0.0.1'}:{SETTINGS.server_port}"


def run_daily(send: bool = True, session_label: str = "每日", session_key: str = "daily", hours: int | None = None) -> dict:
    """执行一次速递流程。

    - session_label: 标题里展示的中文（早间 / 晚间 / 每日 等）
    - session_key: 缓存文件名前缀
    - hours: 抓取窗口（小时），默认读 .env 的 DAILY_WINDOW_HOURS
    """
    hrs = hours if hours is not None else SETTINGS.window_hours
    log.info("=== %s run start (window=%dh) ===", session_label, hrs)
    sn = [a.to_dict() for a in fetch_spacenews(hours=hrs)]
    opml = [e.to_dict() for e in fetch_opml_recent(hours=hrs)]

    # ----- 为国际新闻生成中文翻译页，并把链接重写到 /news/... -----
    batch_id = f"{session_key}_{datetime.now().strftime('%Y-%m-%d')}"
    hero_image_url: str = ""
    if sn:
        try:
            page_map = prepare_news_pages(sn, batch_id=batch_id)
        except Exception as e:
            log.exception("prepare_news_pages failed: %s", e)
            page_map = {}
        public_base = _public_base()
        for a in sn:
            pr = page_map.get(a.get("link", ""))
            if pr:
                a["original_link"] = a["link"]
                a["link"] = f"{public_base}/news/{pr.page_path}"
                if not hero_image_url and pr.image_url:
                    hero_image_url = pr.image_url
        # 若主图仍空，从原始 image_url 中找一张
        if not hero_image_url:
            for a in sn:
                if a.get("image_url"):
                    hero_image_url = a["image_url"]
                    break

    date_str = _today_str()
    if not sn and not opml:
        body = "今日未抓取到任何新文章。"
    else:
        log.info("Summarizing %d SpaceNews + %d OPML entries with %s (session=%s)", len(sn), len(opml), SETTINGS.openai_model, session_label)
        body = daily_summary(sn, opml, session_label=session_label)
    body = _md_links_to_anchor(body)
    summary = _wrap_header(body, date_str, session_label)

    record = {
        "date": date_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "spacenews": sn,
        "opml": opml,
        "summary": summary,
        "sent": False,
    }

    if send:
        try:
            # 把 summary 拆成 “总览段” 与 “新闻列表段”，中间塞一条图片消息
            head, tail = _split_overview_and_list(summary)
            results: list[dict] = []
            results.extend(send_text(head) if head else [])
            if hero_image_url:
                img_res = send_image(hero_image_url)
                if img_res is not None:
                    results.append(img_res)
                else:
                    log.info("hero image send skipped (upload failed)")
            if tail:
                results.extend(send_text(tail))
            ok = all(r.get("errcode") == 0 for r in results) if results else False
            record["sent"] = ok
            record["send_response"] = results
            record["hero_image_url"] = hero_image_url
            if not ok:
                log.error("WeCom send returned non-zero errcode: %s", results)
        except Exception as e:
            log.exception("send pipeline failed: %s", e)
            record["send_error"] = str(e)

    p = cache_path(date_str, session_key)
    p.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Cached -> %s", p)
    log.info("=== %s run done (sent=%s) ===", session_label, record["sent"])
    return record
