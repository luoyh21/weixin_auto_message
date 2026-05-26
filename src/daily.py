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

from .wecom import send_text, send_image, send_news
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


_WP_THUMB_RE = re.compile(r"-(\d{2,4})x(\d{2,4})(?=\.[a-zA-Z]{2,4}(?:\?|$))")


def _upgrade_image_to_full(url: str) -> str:
    """把 WordPress 风格的缩略图 URL 升级为原图。

    例如：
        foo-300x169.png            -> foo.png
        foo-1024x576.jpg?ssl=1     -> foo.jpg?ssl=1
    其它形态原样返回。
    """
    if not url:
        return url
    return _WP_THUMB_RE.sub("", url)


def _is_spacenews_source(article: dict) -> bool:
    src = (article.get("source") or "").lower()
    img = (article.get("image_url") or "").lower()
    link = (article.get("link") or article.get("original_link") or "").lower()
    return ("spacenews" in src) or ("spacenews.com" in img) or ("spacenews.com" in link)


def _pick_hero(articles: list[dict]) -> tuple[dict | None, list[tuple[str, str]]]:
    """返回 (hero_article, image_candidates)。
    hero_article: 第一篇带图、被选作首图的文章 dict（含 link/title/...）
    image_candidates: [(image_url, referer)] 按 SpaceNews 优先的顺序，供上传图片消息时兜底。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    hero: dict | None = None

    def _add(a: dict):
        nonlocal hero
        url = a.get("image_url") or ""
        if not url:
            return
        full = _upgrade_image_to_full(url)
        ref = a.get("original_link") or a.get("link") or ""
        added = False
        for u in (full, url):
            if u and u not in seen:
                out.append((u, ref))
                seen.add(u)
                added = True
        if added and hero is None:
            hero = a

    for a in articles:
        if _is_spacenews_source(a):
            _add(a)
    for a in articles:
        if not _is_spacenews_source(a):
            _add(a)
    return hero, out


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
                # 把翻译页里选定的「主图」回填到文章上，方便后面挑封面
                if pr.image_url and not a.get("image_url"):
                    a["image_url"] = pr.image_url
    hero_article, hero_candidates = _pick_hero(sn)
    hero_image_url = hero_candidates[0][0] if hero_candidates else ""

    date_str = _today_str()
    if not sn and not opml:
        body_md = "今日未抓取到任何新文章。"
    else:
        log.info("Summarizing %d SpaceNews + %d OPML entries with %s (session=%s)", len(sn), len(opml), SETTINGS.openai_model, session_label)
        body_md = daily_summary(sn, opml, session_label=session_label)
    summary_md = _wrap_header(body_md, date_str, session_label)
    summary = _wrap_header(_md_links_to_anchor(body_md), date_str, session_label)

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
            head, tail = _split_overview_and_list(summary)
            head_md, tail_md = _split_overview_and_list(summary_md)
            results: list[dict] = []
            # ---------- 1) 横幅 + 总览（text） ----------
            if head:
                results.extend(send_text(head))
            # ---------- 2) news 图文消息：首卡=大图+第一新闻；其余为列表 ----------
            link_items: list[dict] = [
                {"cn_title": m.group(1).strip(), "url": m.group(2).strip()}
                for m in _MD_LINK_RE.finditer(tail_md)
            ]
            if hero_article and link_items:
                hero_url = hero_article.get("link", "")
                # 把 hero 文章重排到首位
                link_items.sort(key=lambda x: 0 if x["url"] == hero_url else 1)
            news_cards: list[dict] = []
            for i, it in enumerate(link_items[:8]):
                card: dict = {"title": it["cn_title"][:120], "url": it["url"]}
                if i == 0 and hero_image_url:
                    card["picurl"] = hero_image_url
                news_cards.append(card)

            news_ok = False
            if news_cards:
                try:
                    news_res = send_news(news_cards)
                    if news_res and news_res.get("errcode") == 0:
                        results.append(news_res)
                        news_ok = True
                    else:
                        log.warning("send_news non-zero, will fallback: %s", news_res)
                except Exception as e:
                    log.exception("send_news raised, will fallback: %s", e)

            # ---------- Fallback: news 失败时再补图+列表（text 总览已发） ----------
            if not news_ok:
                log.info("news send failed, fallback to image+text-list flow")
                if hero_candidates:
                    img_res = send_image(candidates=hero_candidates)
                    if img_res is not None:
                        results.append(img_res)
                    else:
                        log.info("hero image send skipped (all %d candidates failed)", len(hero_candidates))
                if tail:
                    results.extend(send_text(tail))

            ok = all(r.get("errcode") == 0 for r in results) if results else False
            record["sent"] = ok
            record["send_response"] = results
            record["hero_image_url"] = hero_image_url
            record["used_news_msgtype"] = news_ok
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
