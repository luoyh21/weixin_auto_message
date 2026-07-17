"""每日任务：抓取 -> 总结 -> 缓存 -> 推送企业微信。"""
from __future__ import annotations

import json
import logging
import time
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
from .news_pages import prepare_news_pages, to_beijing
from .extra_news import fetch_all as fetch_extra_news
from .douyin import fetch_douyin_recent
from . import tagging, dedup
from .img_proxy import proxify as proxy_img, prefetch as prefetch_img, cached_bytes as cached_img_bytes
from .dy_pages import render_landing as render_dy_landing
from . import wx_mp
from . import news_archive

log = logging.getLogger(__name__)


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _source_bucket(a: dict) -> str:
    """把一篇国际新闻归入三源之一：SpaceNews / NASA / ESA。"""
    s = (a.get("source") or "").strip().upper()
    if s == "NASA":
        return "NASA"
    if s == "ESA":
        return "ESA"
    return "SpaceNews"  # SpaceNews / NASASpaceflight / spacelive 等聚合源


def _even_split(groups: dict[str, list], total: int) -> list:
    """三源轮转取数，尽量均分到 total 条（各源内部保持原序）。"""
    order = ["SpaceNews", "NASA", "ESA"]
    queues = {k: list(groups.get(k, [])) for k in order}
    out: list = []
    while len(out) < total and any(queues[k] for k in order):
        for k in order:
            if len(out) >= total:
                break
            if queues[k]:
                out.append(queues[k].pop(0))
    return out


def _chunk_balanced(items: list, limit: int = 8) -> list[list]:
    """把 items 切成每条 ≤limit 的若干块，块数最少且尽量均衡（如 12→6+6，14→7+7）。"""
    import math
    n = len(items)
    if n == 0:
        return []
    if n <= limit:
        return [items]
    msgs = math.ceil(n / limit)
    size = math.ceil(n / msgs)
    return [items[i:i + size] for i in range(0, n, size)]


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


def _tag_line_html(tags: list[str]) -> str:
    """正文开头的标签行 HTML（全部标签）。"""
    if not tags:
        return ""
    import html as _h
    spans = "".join(
        f'<span style="display:inline-block;background:#eef2ff;color:#1664ff;font-size:13px;'
        f'padding:2px 10px;border-radius:12px;margin:0 6px 6px 0;">#{_h.escape(t)}</span>'
        for t in tags
    )
    return f'<p style="margin:0 0 10px;">{spans}</p>'


def _build_mpnews_content_sn(
    *,
    title_zh: str,
    body_zh: str,
    source: str,
    published: str,
    inline_img_url: str,
    original_link: str,
    tags: list[str] | None = None,
    summary_zh: str = "",
) -> tuple[str, str]:
    """SpaceNews 文章 → mpnews 正文 HTML + digest。"""
    import html as _h
    pieces: list[str] = []
    if tags:
        pieces.append(_tag_line_html(tags))
    pieces.append(
        f'<p style="color:#8a8f99;font-size:13px;margin:0 0 12px;">'
        f'来源：{_h.escape(source or "SpaceNews")} · {_h.escape(to_beijing(published or ""))}</p>'
    )
    if inline_img_url:
        pieces.append(
            f'<p><img src="{_h.escape(inline_img_url)}" style="max-width:100%;height:auto;"/></p>'
        )
    blurb = (summary_zh or "").strip()
    if blurb:
        pieces.append(
            '<div style="background:#f5f8ff;border:1px solid #e6eeff;border-radius:8px;'
            'padding:12px 14px;margin:0 0 16px;">'
            '<p style="color:#1664ff;font-size:13px;font-weight:600;margin:0 0 6px;">内容概要</p>'
            f'<p style="font-size:15px;line-height:1.75;color:#2a3344;margin:0;">'
            f'{_h.escape(blurb)}</p></div>'
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
    digest = (blurb or body_zh or title_zh or "").strip().replace("\n", " ")[:120]
    return content, digest


def _build_mpnews_content_opml(e: dict, inline_img_url: str) -> tuple[str, str]:
    """公众号订阅条目 → mpnews 正文 HTML + digest。"""
    import html as _h
    pieces: list[str] = []
    src = e.get("source") or "公众号"
    pub = e.get("published") or ""
    pieces.append(
        f'<p style="color:#8a8f99;font-size:13px;margin:0 0 12px;">'
        f'来源：{_h.escape(src)} · {_h.escape(to_beijing(pub))}</p>'
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


def _build_mpnews_content_dy(d, *, landing_base: str) -> tuple[str, str]:
    """抖音作品 → mpnews 正文 HTML + digest。

    正文不再显示任何裸 URL / 口令文本，只放两个按钮形态的 <a>，
    分别指向落地页的自动复制视图：
      /dy/<aweme_id>?copy=url   → 自动复制浏览器链接 + 弹窗提示去浏览器粘贴
      /dy/<aweme_id>?copy=text  → 自动复制抖音口令 + 弹窗提示回抖音 App 搜索
    """
    import html as _h
    pieces: list[str] = []
    pieces.append(
        f'<p style="color:#8a8f99;font-size:13px;margin:0 0 12px;">'
        f'抖音·{_h.escape(d.source)} · {_h.escape(to_beijing(d.published))}</p>'
    )
    if d.desc:
        pieces.append(
            f'<p style="font-size:16px;line-height:1.75;margin:0 0 18px;">{_h.escape(d.desc)}</p>'
        )

    btn_base = (
        "display:block;text-align:center;text-decoration:none;"
        "padding:13px 16px;margin:12px 0 0;border-radius:24px;"
        "font-size:15px;font-weight:600;letter-spacing:.5px;"
    )
    btn_primary = btn_base + "background:#ff0050;color:#fff;"
    btn_secondary = (
        btn_base + "background:#fff;color:#ff0050;border:1.5px solid #ff0050;"
    )
    hint_style = "color:#8a8f99;font-size:12px;text-align:center;margin:8px 4px 0;line-height:1.6;"

    url_link = f"{landing_base}/dy/{_h.escape(d.aweme_id)}?copy=url"
    text_link = f"{landing_base}/dy/{_h.escape(d.aweme_id)}?copy=text"
    pieces.append(
        f'<p><a href="{url_link}" style="{btn_primary}">📋 复制浏览器播放链接</a></p>'
        f'<p style="{hint_style}">点击后会自动复制视频网页链接，按弹窗提示粘贴到浏览器即可播放。</p>'
    )
    pieces.append(
        f'<p><a href="{text_link}" style="{btn_secondary}">📋 复制抖音口令</a></p>'
        f'<p style="{hint_style}">点击后会自动复制抖音口令，按弹窗提示回抖音 App 在搜索框粘贴即可跳转该作品。</p>'
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


def _promo_news_card() -> dict:
    """订阅引导做成「外链卡片」，比正文里的 <a> 链接更不易被企业微信过滤，且可点。

    点击进入 /join 页（展示微信插件关注二维码），卡片缩略图直接用 /join.png。
    """
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/") or _public_base_raw()
    return {
        "title": "📡 订阅 · 邀同好接收每日航天速递",
        "description": "两步开通：① 微信授权一键加入 ② 关注微信插件，点开查看",
        "url": f"{base}/join",
        "picurl": f"{base}/join.png",
    }


def _promo_mpnews_article() -> tuple[dict, dict | None]:
    """订阅引导做成 mpnews 文章（可并入主图文消息当最后一栏，原生渲染含两张二维码）。

    返回 (外链卡片回退, mpnews 文章)；mpnews 构建失败时第二项为 None。
    """
    card = _promo_news_card()
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/") or _public_base_raw()
    try:
        from .join_qr import ensure_qrcode, IMG_FILE as PLUGIN_IMG
        ensure_qrcode()

        thumb_bytes = PLUGIN_IMG.read_bytes() if PLUGIN_IMG.exists() else _placeholder_thumb_bytes("订阅航天速递")
        thumb_id = upload_temp_image_bytes(thumb_bytes) if thumb_bytes else None
        if not thumb_id:
            return card, None

        pl_inline = upload_inline_image_bytes(PLUGIN_IMG.read_bytes()) if PLUGIN_IMG.exists() else ""

        def _img(u: str) -> str:
            return f'<p style="text-align:center;"><img src="{u}" style="max-width:62%;height:auto;"/></p>' if u else ""

        content = (
            '<p style="color:#8a8f99;font-size:13px;margin:0 0 12px;">订阅 · 每日航天速递</p>'
            '<p style="font-size:16px;line-height:1.8;margin:0 0 16px;">'
            '把这条转给同好，按两步即可在微信里每天收到航天要闻速递：</p>'
            '<p style="font-size:15px;font-weight:600;margin:0 0 4px;">第一步 · 加入企业</p>'
            '<p style="color:#8a8f99;font-size:13px;margin:0 0 10px;">'
            f'打开下方订阅页，点「微信一键加入」按微信授权用绑定手机号加入（成为成员才能收推送）。</p>'
            f'<p style="margin:0 0 18px;"><a href="{base}/join">👉 点此打开订阅页，第一步微信授权加入</a></p>'
            '<p style="font-size:15px;font-weight:600;margin:18px 0 4px;">第二步 · 关注微信插件</p>'
            '<p style="color:#8a8f99;font-size:13px;margin:0 0 8px;">'
            '完成第一步后再扫此码，在微信里接收消息（已加入企业不会再要求验证手机号）</p>'
            f'{_img(pl_inline)}'
            f'<p style="margin-top:22px;border-top:1px solid #eee;padding-top:12px;">'
            f'<a href="{base}/join">打开网页版订阅页（二维码更清晰、可放大）</a></p>'
        )
        article = {
            "title": card["title"][:120],
            "thumb_media_id": thumb_id,
            "author": "航天速递",
            "content_source_url": f"{base}/join",
            "content": content,
            "digest": card["description"][:120],
        }
        return card, article
    except Exception as e:
        log.warning("build promo mpnews failed, fallback to link card: %s", e)
        return card, None


def _public_base_raw() -> str:
    return f"http://{SETTINGS.server_host if SETTINGS.server_host != '0.0.0.0' else '127.0.0.1'}:{SETTINGS.server_port}"


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

    # ----- 额外英文新闻源：NASA + ESA 官网 RSS（与 SpaceNews 同流程处理） -----
    try:
        extra = fetch_extra_news(hours=hrs)
        if extra:
            existing_links = {a.get("link", "") for a in sn}
            extra = [x for x in extra if x.get("link") and x["link"] not in existing_links]
            sn.extend(extra)
            log.info("added %d NASA/ESA articles (sn total=%d)", len(extra), len(sn))
    except Exception as e:
        log.exception("fetch NASA/ESA failed: %s", e)

    # 永久归档原始抓取结果（在推送去重之前）；归档不参与日常展示或 cleanup。
    news_archive.append("intl", sn)

    # 公众号订阅按"早间一次性覆盖昨天到今天 24h"的策略：
    # - 早间速递：固定取过去 24 小时（覆盖前一天 8:00 至当天 8:00 这段所有更新）
    # - 晚间速递：完全不发公众号（避免一日内重复且打扰）
    # - 其它（手工 run_once --session daily 等）：复用 SpaceNews 同窗口
    # 公众号(OPML)一天只抓一次（早间），用较大窗口兜底 zlzchat 聚合器的抓取延迟；
    # 即便文章晚几天才进 feed，只要落在窗口内就能补上，dedup 保证不会重复推送。
    if session_key == "morning":
        opml_hours = int(os.getenv("OPML_MORNING_HOURS", "96") or 96)
    elif session_key == "evening":
        opml_hours = 0
    else:
        opml_hours = hrs
    if opml_hours > 0:
        opml = [e.to_dict() for e in fetch_opml_recent(hours=opml_hours)]
        news_archive.append("gzh", opml)
        # 抓到即入独立公众号库（dedup 之前），保证每条更新都能进小程序，
        # 不受「推送去重」或「当次是否被选入摘要」影响。
        try:
            from . import gzh_store
            gzh_store.add(opml)
        except Exception as e:
            log.warning("gzh_store.add failed: %s", e)
    else:
        log.info("opml skipped this session (%s)", session_key)
        opml = []

    # ----- 去重：剔除「上次推送之前 / 已推过 / 与历史高度雷同」的条目，再去同次跨源重复 -----
    last_push = dedup.last_push_at()
    log.info("dedup: last_push_at=%s; before: sn=%d opml=%d", last_push, len(sn), len(opml))
    sn = dedup.dedup_within(dedup.filter_new(sn))
    opml = dedup.dedup_within(dedup.filter_new(opml))
    log.info("dedup: after: sn=%d opml=%d", len(sn), len(opml))

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
                if pr.summary_zh:
                    a["summary_zh"] = pr.summary_zh
                if pr.tags:
                    a["tags"] = pr.tags
    hero_article, hero_candidates = _pick_hero(sn)
    hero_image_url = hero_candidates[0][0] if hero_candidates else ""

    # ----- 抖音账号近 N 小时作品（作为卡片附加） -----
    # 按班次区分窗口，避免两班重复：早间(08:00)回溯 16h→到前一天 16:00；晚间(16:00)回溯 8h→到当天 08:00。
    # 两班首尾衔接、刚好无缝且不重叠。其它会话回落到 DOUYIN_WINDOW_HOURS 或抓取窗口。
    if session_key == "morning":
        dy_hours = int(os.getenv("DOUYIN_MORNING_HOURS", "16") or 16)
    elif session_key == "evening":
        dy_hours = int(os.getenv("DOUYIN_EVENING_HOURS", "8") or 8)
    else:
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

    # 抖音同样去重（按 link/标题/发布时间）
    if douyin_items:
        _dy_tmp = [{"link": d.link, "title": d.title, "published": d.published, "_obj": d} for d in douyin_items]
        _dy_tmp = dedup.filter_new(_dy_tmp)
        douyin_items = [x["_obj"] for x in _dy_tmp]

    douyin_dicts = [d.to_dict() for d in douyin_items]
    news_archive.append("douyin", douyin_dicts)

    date_str = _today_str()
    if not sn:
        # 没有"网页抓取"的国际新闻：不写总览，也不输出"今日未抓取到任何新文章"，
        # 只保留速递第一句（横幅）。公众号/抖音(若有)仍照常作为卡片发出；
        # 若三者皆空，后续 has_real_content 判定会整条跳过不发。
        body_md = ""
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
            CARD_LIMIT = 8  # 企业微信单条 news/mpnews 硬上限
            results: list[dict] = []

            # ---------- 国际新闻：三源(SpaceNews/NASA/ESA)均分 → mpnews ----------
            # 卡片直接由抓取+翻译成功的文章生成（不依赖 GPT 列表），便于按来源均分。
            groups: dict[str, list[dict]] = {"SpaceNews": [], "NASA": [], "ESA": []}
            for a in sn:
                if not a.get("link"):
                    continue
                if not (a.get("body_zh") or "").strip():
                    log.info("skip intl (no zh body): %s", a.get("original_link") or a.get("link"))
                    continue
                groups[_source_bucket(a)].append(a)
            selected = _even_split(groups, max(1, SETTINGS.news_max_total))
            log.info(
                "intl cards: SpaceNews=%d NASA=%d ESA=%d -> selected=%d (max=%d)",
                len(groups["SpaceNews"]), len(groups["NASA"]), len(groups["ESA"]),
                len(selected), SETTINGS.news_max_total,
            )
            record["intl_counts"] = {k: len(v) for k, v in groups.items()}
            record["intl_selected"] = len(selected)

            hero_ref0 = (hero_article or {}).get("original_link") if hero_article else ""

            def _build_intl(a: dict, use_hero: bool) -> tuple[dict, dict | None]:
                title = (a.get("title_zh") or a.get("title") or "").strip()
                tags = a.get("tags") or tagging.tags_for(title, scope="国际新闻")
                titled = (tagging.tag_prefix(tags) + title).strip()  # 标题只放一个标签
                card: dict = {"title": titled[:120], "url": a.get("link", "")}
                src_img = _upgrade_image_to_full(a.get("image_url") or "")
                src_ref = a.get("original_link") or a.get("link") or ""
                if use_hero and hero_image_url:
                    src_img, src_ref = hero_image_url, (hero_ref0 or src_ref)
                if src_img and prefetch_img(src_img, src_ref):
                    card["picurl"] = proxy_img(src_img, src_ref)
                thumb_bytes = cached_img_bytes(src_img, src_ref) if src_img else None
                if not thumb_bytes:
                    thumb_bytes = _placeholder_thumb_bytes(title)
                thumb_id = upload_temp_image_bytes(thumb_bytes) if thumb_bytes else None
                inline_url = upload_inline_image_bytes(thumb_bytes) if thumb_bytes else None
                mp_art = None
                if thumb_id:
                    content, digest = _build_mpnews_content_sn(
                        title_zh=title,
                        body_zh=a.get("body_zh") or "",
                        source=a.get("source") or "SpaceNews",
                        published=a.get("published") or "",
                        inline_img_url=inline_url or "",
                        original_link=a.get("original_link") or "",
                        tags=tags,  # 正文开头放全部标签
                        summary_zh=a.get("summary_zh") or "",
                    )
                    mp_art = {
                        "title": titled[:120],
                        "thumb_media_id": thumb_id,
                        "author": a.get("source") or "SpaceNews",
                        "content_source_url": a.get("original_link") or a.get("link"),
                        "content": content,
                        "digest": digest,
                    }
                return card, mp_art

            # (fallback_card, mpnews_article|None) 成对，便于按消息分块
            items: list[tuple[dict, dict | None]] = []
            for i, a in enumerate(selected):
                items.append(_build_intl(a, use_hero=(i == 0)))

            # ---- 抖音作为附加卡片接在国际新闻之后（标签 #航天视频）----
            dy_reserve = min(len(douyin_items), max(0, dy_max))
            for d in douyin_items[:dy_reserve]:
                dtitled = ("#航天视频 " + (d.title or f"抖音·{d.source}")).strip()
                thumb_bytes3 = _placeholder_thumb_bytes(f"抖音·{d.source}")
                thumb_id3 = upload_temp_image_bytes(thumb_bytes3) if thumb_bytes3 else None
                try:
                    render_dy_landing(
                        d.aweme_id, title=d.title, source=d.source, published=d.published,
                        share_text=d.share_text, share_url=d.share_url or d.link, image_proxy_url="",
                    )
                except Exception as e:
                    log.warning("render dy landing failed for %s: %s", d.aweme_id, e)
                dy_landing_url = f"{_public_base()}/dy/{d.aweme_id}"
                fb3 = {"title": dtitled[:120], "description": d.published, "url": dy_landing_url}
                mp3 = None
                if thumb_id3:
                    content3, digest3 = _build_mpnews_content_dy(d, landing_base=_public_base())
                    mp3 = {
                        "title": dtitled[:120], "thumb_media_id": thumb_id3,
                        "author": f"抖音·{d.source}", "content_source_url": "",
                        "content": content3, "digest": digest3,
                    }
                items.append((fb3, mp3))

            # ---- 公众号 → 单独一条外链 news 消息（标签 #国内航天）----
            extra_news_cards: list[dict] = []
            opml_cap = int(os.getenv("OPML_MAX_CARDS", "6") or 6)
            for e in opml[: max(0, opml_cap)]:
                if not e.get("link"):
                    continue
                etitle = e.get("title") or ""
                etags = tagging.tags_for(f"{etitle} {e.get('description', '')}", scope="国内航天")
                c2: dict = {
                    "title": (tagging.tag_prefix(etags) + etitle).strip()[:120],
                    "url": e.get("link", ""),
                    "description": e.get("source") or "公众号",
                }
                img = (e.get("image_url") or "").strip()
                if img and prefetch_img(img, e.get("link") or ""):
                    c2["picurl"] = proxy_img(img, e.get("link") or "")
                extra_news_cards.append(c2)

            # ---------- 真实内容判定：订阅引导卡不算内容 ----------
            # items=国际(mpnews)+抖音；extra_news_cards=公众号。两者全空即"今日无文章"。
            has_real_content = bool(items) or bool(extra_news_cards)

            # ---------- 无任何真实文章：整条都不发（连总览/订阅卡都不发）----------
            if not has_real_content:
                log.info("no real articles this session; skip push entirely (promo not sent alone)")
                record["sent"] = False
                record["skipped"] = "no-new-content"
                record["msg_chunks"] = 0
            else:
                # ---------- 订阅引导：上午(morning)不发；下午(evening)及手动固定发 ----------
                promo_card: dict | None = None
                promo_article: dict | None = None
                if session_key != "morning":
                    promo_card, promo_article = _promo_mpnews_article()

                chunks = _chunk_balanced(items, CARD_LIMIT)
                if promo_card is not None and not extra_news_cards:
                    if chunks:
                        if len(chunks[-1]) >= CARD_LIMIT:
                            chunks[-1] = chunks[-1][:CARD_LIMIT - 1]
                        chunks[-1].append((promo_card, promo_article))
                    else:
                        chunks = [[(promo_card, promo_article)]]
                    promo_card = None  # 已消费

                record["msg_chunks"] = len(chunks)
                record["promo_placed"] = "skipped-morning" if session_key == "morning" else "appended-last"


                head, tail = _split_overview_and_list(summary)
                if head:
                    results.extend(send_text(head))
                    # 文字与首条图文之间也留间隔，保证「先文字后图文」顺序
                    if chunks or extra_news_cards:
                        time.sleep(2)

                news_ok = False
                used_mpnews = False
                for ci, chunk in enumerate(chunks):
                    if ci > 0:
                        time.sleep(2)
                    arts = [mp for (_c, mp) in chunk]
                    fbs = [c for (c, _mp) in chunk]
                    sent = False
                    if arts and all(x is not None for x in arts):
                        try:
                            mp_res = send_mpnews(arts)
                            if mp_res and mp_res.get("errcode") == 0:
                                results.append(mp_res)
                                news_ok = True
                                used_mpnews = True
                                sent = True
                            else:
                                log.warning("send_mpnews non-zero, fallback news: %s", mp_res)
                        except Exception as e:
                            log.exception("send_mpnews raised: %s", e)
                    if not sent and fbs:
                        try:
                            n_res = send_news(fbs)
                            if n_res and n_res.get("errcode") == 0:
                                results.append(n_res)
                                news_ok = True
                            else:
                                log.warning("send_news non-zero: %s", n_res)
                        except Exception as e:
                            log.exception("send_news raised: %s", e)

                if extra_news_cards or promo_card is not None:
                    if promo_card is not None:
                        del extra_news_cards[CARD_LIMIT - 1:]
                        extra_news_cards.append(promo_card)
                    try:
                        time.sleep(2)
                        ex_res = send_news(extra_news_cards)
                        if ex_res and ex_res.get("errcode") == 0:
                            results.append(ex_res)
                        else:
                            log.warning("send_news(extra) non-zero: %s", ex_res)
                    except Exception as e:
                        log.exception("send_news(extra) raised: %s", e)

                if not news_ok:
                    log.info("intl send failed, fallback to image+text-list flow")
                    if hero_candidates:
                        img_res = send_image(candidates=hero_candidates)
                        if img_res is not None:
                            results.append(img_res)
                    if tail:
                        results.extend(send_text(tail))

                ok = all(r.get("errcode") == 0 for r in results) if results else False
                record["sent"] = ok
                record["send_response"] = results
                record["hero_image_url"] = hero_image_url
                record["used_news_msgtype"] = news_ok
                record["used_mpnews"] = used_mpnews
                if ok:
                    pushed = list(sn) + list(opml) + [
                        {"link": d.link, "title": d.title} for d in douyin_items[:dy_reserve]
                    ]
                    try:
                        dedup.record(pushed)
                    except Exception as e:
                        log.warning("dedup.record failed: %s", e)
                else:
                    log.error("WeCom send returned non-zero errcode: %s", results)
        except Exception as e:
            log.exception("send pipeline failed: %s", e)
            record["send_error"] = str(e)

        # ---------- 同步一份聚合图文到公众号（无新内容时不同步） ----------
        if record.get("skipped") == "no-new-content":
            log.info("skip wx_mp sync (no new content)")
        else:
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
    # 永久保留最终的推送/翻译结果；短期 cache 文件仍可由 cleanup 轮转。
    news_archive.append("digest", [{
        "cache_file": p.name,
        "session": session_key,
        "generated_at": record.get("generated_at", ""),
        "record": record,
    }])
    log.info("Cached -> %s", p)
    log.info("=== %s run done (sent=%s) ===", session_label, record["sent"])
    return record
