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

from .wecom import (
    send_text, send_image, send_news, send_mpnews,
    upload_temp_image_bytes, upload_inline_image_bytes,
)
from .news_pages import prepare_news_pages
from .douyin import fetch_douyin_recent
from .img_proxy import proxify as proxy_img, prefetch as prefetch_img, cached_bytes as cached_img_bytes
from .dy_pages import render_landing as render_dy_landing
from . import wx_mp

log = logging.getLogger(__name__)


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _slug_of(url: str) -> str:
    """取 URL path 的最后一段（lower），用于在 GPT 偶尔改坏 URL 时按 slug 重对齐。"""
    try:
        from urllib.parse import urlparse
        return (urlparse(url).path.rsplit("/", 1)[-1] or "").lower()
    except Exception:
        return ""


def _repair_summary_urls(text: str, known_urls: list[str]) -> str:
    """LLM 偶尔会把 URL 缩写或漏字（例如 morning_2026-05-28 → morning_05-28），
    把摘要里所有 markdown link 的 URL 用"末段 slug 完全相等"的方式重对齐到真实 URL。"""
    if not known_urls:
        return text
    by_slug: dict[str, str] = {}
    for u in known_urls:
        slug = _slug_of(u)
        if slug:
            by_slug.setdefault(slug, u)

    def _fix(m: re.Match) -> str:
        title, url = m.group(1), m.group(2)
        if url in known_urls:
            return m.group(0)
        good = by_slug.get(_slug_of(url))
        if good and good != url:
            log.info("repaired summary URL: %s -> %s", url, good)
            return f"[{title}]({good})"
        return m.group(0)

    return _MD_LINK_RE.sub(_fix, text)


def _md_links_to_anchor(text: str) -> str:
    """把 markdown `[text](url)` 转成企业微信文本支持的 `<a href="url">text</a>`。

    顺便兜底：把残留的孤立裸 URL 用 <a> 包一层，避免在客户端只展示纯文本。
    """
    text = _MD_LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    return text


def _placeholder_thumb_bytes(title: str) -> bytes:
    """没拿到封面图时生成一个深色渐变 + 标题文字的占位 JPEG。mpnews 强制要求 thumb。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io
        w, h = 900, 500
        img = Image.new("RGB", (w, h), (10, 20, 38))
        d = ImageDraw.Draw(img)
        for y in range(h):
            t = y / h
            r = int(10 + (20 - 10) * t)
            g = int(20 + (60 - 20) * t)
            b = int(38 + (110 - 38) * t)
            d.line([(0, y), (w, y)], fill=(r, g, b))
        try:
            font = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 36)
        except Exception:
            font = ImageFont.load_default()
        text = (title or "航天速递").strip()[:24]
        bbox = d.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((w - tw) // 2, (h - th) // 2), text, fill=(245, 246, 250), font=font)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception:
        return b""


def _para_html(text: str) -> str:
    """纯文本按空行切段，渲染成 <p>，转义 HTML 实体。"""
    import html as _h
    parts = [p.strip() for p in re.split(r"\n{2,}", text or "") if p.strip()]
    return "\n".join(f"<p>{_h.escape(p)}</p>" for p in parts)


def _build_mpnews_content_sn(
    *,
    title_zh: str,
    body_zh: str,
    source: str,
    published: str,
    inline_img_url: str,
    original_link: str,
) -> tuple[str, str]:
    """SpaceNews 文章 → mpnews 正文 HTML + digest。"""
    import html as _h
    pieces: list[str] = []
    pieces.append(
        f'<p style="color:#8a8f99;font-size:13px;margin:0 0 12px;">'
        f'来源：{_h.escape(source or "SpaceNews")} · {_h.escape(published or "")}</p>'
    )
    if inline_img_url:
        pieces.append(
            f'<p><img src="{_h.escape(inline_img_url)}" style="max-width:100%;height:auto;"/></p>'
        )
    if body_zh:
        pieces.append(f'<div style="font-size:16px;line-height:1.75;">{_para_html(body_zh)}</div>')
    else:
        pieces.append('<p style="color:#8c4a00;">原文正文未能抓取，请点击下方「阅读原文」查看英文版。</p>')
    if original_link:
        pieces.append(
            f'<p style="color:#8a8f99;font-size:13px;margin-top:24px;border-top:1px solid #eee;padding-top:12px;">'
            f'原文链接：{_h.escape(original_link)}</p>'
        )
    content = "\n".join(pieces)
    digest = (body_zh or title_zh or "").strip().replace("\n", " ")[:120]
    return content, digest


def _build_mpnews_content_opml(e: dict, inline_img_url: str) -> tuple[str, str]:
    """公众号订阅条目 → mpnews 正文 HTML + digest。"""
    import html as _h
    pieces: list[str] = []
    src = e.get("source") or "公众号"
    pub = e.get("published") or ""
    pieces.append(
        f'<p style="color:#8a8f99;font-size:13px;margin:0 0 12px;">'
        f'来源：{_h.escape(src)} · {_h.escape(pub)}</p>'
    )
    if inline_img_url:
        pieces.append(
            f'<p><img src="{_h.escape(inline_img_url)}" style="max-width:100%;height:auto;"/></p>'
        )
    desc = (e.get("description") or "").strip()
    if desc:
        pieces.append(f'<div style="font-size:16px;line-height:1.75;">{_para_html(desc)}</div>')
    link = e.get("link") or ""
    if link:
        pieces.append(
            f'<p style="margin-top:24px;"><a href="{_h.escape(link)}">在公众号内打开原文</a></p>'
        )
    content = "\n".join(pieces)
    digest = (desc or e.get("title") or "")[:120]
    return content, digest


def _build_mpnews_content_dy(d, inline_img_url: str) -> tuple[str, str]:
    """抖音作品 → mpnews 正文 HTML + digest。
    包含：封面 + 描述 + 浏览器播放链接 + 抖音口令（用于回 App 打开）。"""
    import html as _h
    pieces: list[str] = []
    pieces.append(
        f'<p style="color:#8a8f99;font-size:13px;margin:0 0 12px;">'
        f'来源：抖音·{_h.escape(d.source)} · {_h.escape(d.published)}</p>'
    )
    if inline_img_url:
        pieces.append(
            f'<p><img src="{_h.escape(inline_img_url)}" style="max-width:100%;height:auto;"/></p>'
        )
    if d.desc:
        pieces.append(
            f'<p style="font-size:16px;line-height:1.75;">{_h.escape(d.desc)}</p>'
        )

    box_style = (
        "background:#f5f6f7;padding:12px 14px;border-radius:8px;"
        "font-size:14px;line-height:1.6;word-break:break-all;"
        "-webkit-user-select:text;user-select:text;"
    )
    hint_style = "margin-top:18px;color:#8a8f99;font-size:13px;"
    tip_style = "color:#bbb;font-size:12px;margin:6px 2px 0;"

    share_url = d.share_url or d.link
    if share_url:
        pieces.append(
            f'<p style="{hint_style}">▶︎ 在浏览器中播放：</p>'
            f'<p style="{box_style}"><a href="{_h.escape(share_url)}">{_h.escape(share_url)}</a></p>'
            f'<p style="{tip_style}">企业微信可直接点击打开；在微信中阅读时请长按上方链接选中后复制，再粘贴到浏览器中打开。</p>'
        )

    share = (d.share_text or "").strip()
    if share:
        pieces.append(
            f'<p style="{hint_style}">📨 抖音口令（长按整段选中复制，回到抖音 App 即弹出该视频）：</p>'
            f'<p style="{box_style}">{_h.escape(share)}</p>'
        )
    content = "\n".join(pieces)
    digest = (d.desc or d.title or "")[:120]
    return content, digest


def _md_to_mp_html(summary_md: str, *, hero_img_url: str = "", douyin: list[dict] | None = None) -> str:
    """把摘要 markdown 渲染成可被微信公众号草稿接受的 HTML（仅 <p> / <a> / <img>）。

    - hero_img_url 必须已经是 mmbiz.qpic.cn（uploadimg 返回）的 URL
    - 链接保留原样（mp.weixin / 自建 /news/ 都能跳）
    - 末尾追加抖音卡片清单（只是链接，没办法直接在公众号正文里嵌视频）
    """
    import html as _h

    paras_html: list[str] = []
    if hero_img_url:
        paras_html.append(f'<p><img src="{_h.escape(hero_img_url)}" style="max-width:100%;height:auto;"/></p>')

    for raw in re.split(r"\n{2,}", summary_md.strip()):
        raw = raw.strip()
        if not raw:
            continue
        esc = _h.escape(raw)
        esc = _MD_LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{_h.escape(m.group(1))}</a>', raw)
        paras_html.append(f"<p>{esc}</p>")

    if douyin:
        paras_html.append("<p><strong>🎬 抖音速递</strong></p>")
        for d in douyin:
            title = _h.escape(d.get("title") or "")
            src = _h.escape(d.get("source") or "")
            url = d.get("share_url") or d.get("link") or ""
            paras_html.append(
                f'<p>[{src}] {title}<br/><a href="{_h.escape(url)}">{_h.escape(url)}</a></p>'
            )

    return "\n".join(paras_html)


def _try_publish_to_mp(
    *,
    title: str,
    summary_md: str,
    hero_image_url: str,
    hero_referer: str,
    douyin_dicts: list[dict],
) -> str:
    """尝试发布一篇聚合图文到公众号。失败只打日志、返回空串，不影响主流程。

    走 freepublish/submit ——只产出永久 mp.weixin.qq.com URL，**不会群发到关注者**。
    """
    cfg = wx_mp.load_config()
    if not cfg.enabled:
        return ""
    if not (cfg.appid and cfg.secret):
        log.info("wx_mp disabled: missing WX_MP_APPID/SECRET")
        return ""
    try:
        thumb_bytes = cached_img_bytes(hero_image_url, hero_referer) if hero_image_url else None
        if not thumb_bytes:
            log.warning("wx_mp publish skipped: no hero image bytes available")
            return ""
        token = wx_mp.get_access_token(cfg)
        try:
            hero_mp_url = wx_mp.upload_image_inline(token, thumb_bytes)
        except Exception as e:
            log.warning("uploadimg failed, fall back to no inline hero: %s", e)
            hero_mp_url = ""

        body_html = _md_to_mp_html(summary_md, hero_img_url=hero_mp_url, douyin=douyin_dicts)
        digest_src = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", summary_md)).strip()
        res = wx_mp.publish_aggregate(
            cfg,
            title=title,
            body_html=body_html,
            digest=digest_src,
            thumb_bytes=thumb_bytes,
        )
        if res.url:
            log.info("wx_mp publish ok: %s", res.url)
            return res.url
        if res.needs_manual:
            log.warning("wx_mp draft saved, needs manual publish (draft_id=%s)", res.draft_id)
            try:
                send_text(
                    "📝 公众号草稿已自动写入（你的账号未开通 API 发布权限）。\n"
                    "请到 mp.weixin.qq.com → 草稿箱 → 找到最新一篇 → 点「发表」即可生成 mp 永久链接。"
                )
            except Exception:
                pass
        elif res.error:
            log.warning("wx_mp publish failed: %s", res.error)
        return ""
    except Exception as e:
        log.exception("wx_mp publish failed: %s", e)
        return ""


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

    # 公众号订阅按"早间一次性覆盖昨天到今天 24h"的策略：
    # - 早间速递：固定取过去 24 小时（覆盖前一天 8:00 至当天 8:00 这段所有更新）
    # - 晚间速递：完全不发公众号（避免一日内重复且打扰）
    # - 其它（手工 run_once --session daily 等）：复用 SpaceNews 同窗口
    if session_key == "morning":
        opml_hours = 24
    elif session_key == "evening":
        opml_hours = 0
    else:
        opml_hours = hrs
    if opml_hours > 0:
        opml = [e.to_dict() for e in fetch_opml_recent(hours=opml_hours)]
    else:
        log.info("opml skipped this session (%s)", session_key)
        opml = []

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
                if pr.image_url and not a.get("image_url"):
                    a["image_url"] = pr.image_url
                if pr.title_zh:
                    a["title_zh"] = pr.title_zh
                if pr.body_zh:
                    a["body_zh"] = pr.body_zh
    hero_article, hero_candidates = _pick_hero(sn)
    hero_image_url = hero_candidates[0][0] if hero_candidates else ""

    # ----- 抖音账号近 N 小时作品（作为卡片附加） -----
    dy_hours_env = os.getenv("DOUYIN_WINDOW_HOURS", "").strip()
    dy_hours = int(dy_hours_env) if dy_hours_env else hrs
    dy_max = int(os.getenv("DOUYIN_MAX_TOTAL", "0") or 0)
    dy_per_user = int(os.getenv("DOUYIN_PER_USER_LIMIT", "1") or 1)
    try:
        douyin_items = (
            fetch_douyin_recent(hours=dy_hours, max_total=dy_max, per_user_limit=dy_per_user)
            if dy_max > 0
            else []
        )
    except Exception as e:
        log.exception("fetch_douyin_recent failed: %s", e)
        douyin_items = []

    # 调试 / 演示用：DOUYIN_DEMO_INJECT=1 时，若窗口内没有作品，强行注入"该账号最新一条"
    # 并把 published / create_ts 改写成当前时间，方便预览整套抖音卡片效果。
    if not douyin_items and os.getenv("DOUYIN_DEMO_INJECT", "0").strip() == "1":
        try:
            from .douyin import fetch_user_recent, _parse_users
            users = _parse_users(os.getenv("DOUYIN_USERS", ""))
            for name, sec in users[: max(1, int(os.getenv("DOUYIN_MAX_TOTAL", "1") or 1))]:
                latest = fetch_user_recent(sec, name=name, hours=24 * 365, count=3)
                if latest:
                    d = latest[0]
                    now_dt = datetime.now().astimezone()
                    d.published = now_dt.isoformat(timespec="seconds")
                    d.create_ts = int(now_dt.timestamp())
                    douyin_items.append(d)
            log.info("douyin demo inject -> %d entries", len(douyin_items))
        except Exception as e:
            log.warning("douyin demo inject failed: %s", e)

    douyin_dicts = [d.to_dict() for d in douyin_items]

    date_str = _today_str()
    if not sn and not opml:
        body_md = "今日未抓取到任何新文章。"
    else:
        log.info("Summarizing %d SpaceNews + %d OPML entries with %s (session=%s)", len(sn), len(opml), SETTINGS.openai_model, session_label)
        body_md = daily_summary(sn, opml, session_label=session_label)
    # 用真实文章 URL 修复 GPT 偶尔写错的链接（避免"页面 not found"，并保证后续按 URL 反查封面图能命中）
    known_urls = [a.get("link", "") for a in sn if a.get("link")] + [e.get("link", "") for e in opml if e.get("link")]
    body_md = _repair_summary_urls(body_md, known_urls)
    summary_md = _wrap_header(body_md, date_str, session_label)
    summary = _wrap_header(_md_links_to_anchor(body_md), date_str, session_label)

    record = {
        "date": date_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "spacenews": sn,
        "opml": opml,
        "douyin": douyin_dicts,
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
            # 反查每篇 SpaceNews 文章的封面与 Referer（按重写后的本机 /news/... 链接索引）
            sn_pic_map: dict[str, tuple[str, str]] = {}
            for a in sn:
                img = _upgrade_image_to_full(a.get("image_url") or "")
                if not img:
                    continue
                ref = a.get("original_link") or a.get("link") or ""
                sn_pic_map[a.get("link", "")] = (img, ref)

            # 卡片名额分配（总上限 8）：
            #   - 公众号 优先，最多 OPML_MAX_CARDS（默认 2）；
            #   - 抖音 次之，最多 DOUYIN_MAX_TOTAL；
            #   - 剩余给 SpaceNews。
            # 展示顺序：新闻 → 公众号 → 抖音。
            opml_link_items: list[dict] = [it for it in link_items if "mp.weixin.qq.com" in it["url"]]
            sn_link_items: list[dict] = [it for it in link_items if "mp.weixin.qq.com" not in it["url"]]
            opml_cap = int(os.getenv("OPML_MAX_CARDS", "2") or 2)
            opml_take = opml_link_items[: max(0, opml_cap)]

            # SpaceNews + 抖音 走 mpnews（原生渲染），公众号单独走 msgtype=news
            # 直接跳到 mp.weixin.qq.com 原文。两路消息分两条发出。
            mpnews_articles: list[dict] = []      # SpaceNews + 抖音
            mpnews_cards_fallback: list[dict] = []  # mpnews 失败时回退用的 news 卡片
            opml_news_cards: list[dict] = []      # 公众号 → 直接外链卡片
            sn_by_link = {a.get("link"): a for a in sn}
            opml_by_link = {e.get("link"): e for e in opml}

            dy_reserve = min(len(douyin_items), max(0, dy_max))
            # mpnews 总上限 8 篇：SpaceNews 数 + 抖音数 ≤ 8
            base_limit = max(0, 8 - dy_reserve)

            def _add_pair(card: dict, mp_article: dict | None) -> None:
                mpnews_cards_fallback.append(card)
                if mp_article is not None:
                    mpnews_articles.append(mp_article)

            # ---- 1) SpaceNews ----
            for i, it in enumerate(sn_link_items[:base_limit]):
                card: dict = {"title": it["cn_title"][:120], "url": it["url"]}
                src_img, src_ref = "", ""
                pic_pair = sn_pic_map.get(it["url"])
                if i == 0 and hero_image_url:
                    hero_ref = (hero_article or {}).get("original_link") if hero_article else ""
                    src_img, src_ref = hero_image_url, hero_ref or ""
                elif pic_pair:
                    src_img, src_ref = pic_pair
                if src_img and prefetch_img(src_img, src_ref):
                    card["picurl"] = proxy_img(src_img, src_ref)
                elif src_img:
                    log.info("drop picurl (prefetch failed): %s", src_img)

                # mpnews：用缓存图作 thumb，无缓存图用占位封面；正文嵌中文译文
                sn_a = sn_by_link.get(it["url"], {})
                thumb_bytes = cached_img_bytes(src_img, src_ref) if src_img else None
                if not thumb_bytes:
                    thumb_bytes = _placeholder_thumb_bytes(it["cn_title"])
                thumb_id = upload_temp_image_bytes(thumb_bytes) if thumb_bytes else None
                inline_url = upload_inline_image_bytes(thumb_bytes) if thumb_bytes else None
                mp_art = None
                if thumb_id:
                    content, digest = _build_mpnews_content_sn(
                        title_zh=sn_a.get("title_zh") or it["cn_title"],
                        body_zh=sn_a.get("body_zh") or "",
                        source=sn_a.get("source") or "SpaceNews",
                        published=sn_a.get("published") or "",
                        inline_img_url=inline_url or "",
                        original_link=sn_a.get("original_link") or "",
                    )
                    mp_art = {
                        "title": (sn_a.get("title_zh") or it["cn_title"])[:120],
                        "thumb_media_id": thumb_id,
                        "author": sn_a.get("source") or "SpaceNews",
                        "content_source_url": sn_a.get("original_link") or it["url"],
                        "content": content,
                        "digest": digest,
                    }
                _add_pair(card, mp_art)

            # ---- 2) 公众号：单独走 msgtype=news，直接跳转 mp.weixin.qq.com，不再过中间页 ----
            opml_pic_map: dict[str, tuple[str, str]] = {}
            for e in opml:
                img = (e.get("image_url") or "").strip()
                if img:
                    opml_pic_map[e.get("link", "")] = (img, e.get("link") or "")
            for it in opml_take:
                card2: dict = {
                    "title": f"[公众号] {it['cn_title']}"[:120],
                    "url": it["url"],  # mp.weixin.qq.com 直链
                    "description": (opml_by_link.get(it["url"], {}).get("source") or "公众号"),
                }
                pp = opml_pic_map.get(it["url"])
                if pp and prefetch_img(pp[0], pp[1]):
                    card2["picurl"] = proxy_img(pp[0], pp[1])
                opml_news_cards.append(card2)

            # ---- 3) 抖音 ----
            for d in douyin_items[:dy_reserve]:
                ok_pic = bool(d.image_url) and prefetch_img(d.image_url, "https://www.iesdouyin.com/")
                pic_proxy_url = proxy_img(d.image_url, "https://www.iesdouyin.com/") if ok_pic else ""
                try:
                    render_dy_landing(
                        d.aweme_id,
                        title=d.title,
                        source=d.source,
                        published=d.published,
                        share_text=d.share_text,
                        share_url=d.share_url or d.link,
                        image_proxy_url=pic_proxy_url,
                    )
                    dy_url = f"{_public_base()}/dy/{d.aweme_id}"
                except Exception as e:
                    log.warning("render dy landing failed for %s: %s", d.aweme_id, e)
                    dy_url = d.link
                card3 = {
                    "title": f"[抖音·{d.source}] {d.title}"[:120],
                    "description": d.published,
                    "url": dy_url,
                    "picurl": pic_proxy_url,
                }

                thumb_bytes3 = cached_img_bytes(d.image_url, "https://www.iesdouyin.com/") if d.image_url else None
                if not thumb_bytes3:
                    thumb_bytes3 = _placeholder_thumb_bytes(f"抖音·{d.source}")
                thumb_id3 = upload_temp_image_bytes(thumb_bytes3) if thumb_bytes3 else None
                inline_url3 = upload_inline_image_bytes(thumb_bytes3) if thumb_bytes3 else None
                mp_art3 = None
                if thumb_id3:
                    content3, digest3 = _build_mpnews_content_dy(d, inline_url3 or "")
                    mp_art3 = {
                        "title": f"[抖音·{d.source}] {d.title}"[:120],
                        "thumb_media_id": thumb_id3,
                        "author": f"抖音·{d.source}",
                        "content_source_url": d.share_url or d.link,
                        "content": content3,
                        "digest": digest3,
                    }
                _add_pair(card3, mp_art3)

            news_ok = False
            used_mpnews = False

            # ---- A) SpaceNews + 抖音 → mpnews 原生渲染 ----
            if mpnews_articles and len(mpnews_articles) == len(mpnews_cards_fallback):
                try:
                    mp_res = send_mpnews(mpnews_articles)
                    if mp_res and mp_res.get("errcode") == 0:
                        results.append(mp_res)
                        news_ok = True
                        used_mpnews = True
                    else:
                        log.warning("send_mpnews non-zero, fallback to news: %s", mp_res)
                except Exception as e:
                    log.exception("send_mpnews raised, fallback to news: %s", e)

            if not used_mpnews and mpnews_cards_fallback:
                try:
                    news_res = send_news(mpnews_cards_fallback)
                    if news_res and news_res.get("errcode") == 0:
                        results.append(news_res)
                        news_ok = True
                    else:
                        log.warning("send_news non-zero: %s", news_res)
                except Exception as e:
                    log.exception("send_news raised: %s", e)

            # ---- B) 公众号 → 直链 news 卡片（一条消息）----
            if opml_news_cards:
                try:
                    op_res = send_news(opml_news_cards)
                    if op_res and op_res.get("errcode") == 0:
                        results.append(op_res)
                    else:
                        log.warning("send_news(opml) non-zero: %s", op_res)
                except Exception as e:
                    log.exception("send_news(opml) raised: %s", e)

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
            record["used_mpnews"] = used_mpnews
            if not ok:
                log.error("WeCom send returned non-zero errcode: %s", results)
        except Exception as e:
            log.exception("send pipeline failed: %s", e)
            record["send_error"] = str(e)

        # ---------- 同步一份聚合图文到公众号（不会群发到关注者） ----------
        try:
            hero_ref = (hero_article or {}).get("original_link") if hero_article else ""
            mp_url = _try_publish_to_mp(
                title=f"航天{session_label}速递 {date_str}",
                summary_md=summary_md,
                hero_image_url=hero_image_url,
                hero_referer=hero_ref or "",
                douyin_dicts=douyin_dicts,
            )
            if mp_url:
                record["wx_mp_url"] = mp_url
                try:
                    send_text(f"📰 同步公众号永久链接：\n<a href=\"{mp_url}\">{mp_url}</a>")
                except Exception as e:
                    log.warning("send mp url message failed: %s", e)
        except Exception as e:
            log.exception("wx_mp pipeline failed: %s", e)

    p = cache_path(date_str, session_key)
    p.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Cached -> %s", p)
    log.info("=== %s run done (sent=%s) ===", session_label, record["sent"])
    return record
