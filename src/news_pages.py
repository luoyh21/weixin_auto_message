"""为每篇国际新闻生成一张可访问的中文翻译页。

流程：
1. 调用 prepare_news_pages(articles, batch_id)
   - articles: 来自 spacenews / ingest 的 dict 列表，至少含 title/link/image_url
   - 对每条 article 抓取原文 HTML，提取主体正文与文中图片
   - 把所有正文（带分隔符）拼成一段一次性丢给 GPT 翻译成中文，
     再用分隔符切回每篇 → 显著降低成本
   - 渲染为静态 HTML，存到 data/news_pages/<batch_id>/<slug>.html
   - 维护 manifest.json，仅保留最近 2 个 batch（默认）
2. 返回 dict: {orig_url: page_url}，daily.py 据此重写消息中的链接
"""
from __future__ import annotations

import html
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import SETTINGS
from .summarizer import client as openai_client

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
}

PAGES_ROOT = SETTINGS.cache_dir.parent / "news_pages"
PAGES_ROOT.mkdir(parents=True, exist_ok=True)
MANIFEST = PAGES_ROOT / "manifest.json"
KEEP_BATCHES = 2

# 翻译批切分用的分隔符（要长且不太可能出现在原文里）
ARTICLE_DELIM = "###@@@ARTICLE_BREAK@@@###"


# ---------- 抓原文与提取正文 ----------

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(min=1, max=4),
    retry=retry_if_exception_type((requests.HTTPError, requests.ConnectionError, requests.Timeout)),
    reraise=True,
)
def _http_get(url: str) -> requests.Response:
    # 加上 Referer = 自身 origin，绕开部分站的简单防盗链/直连过滤
    h = dict(HEADERS)
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        if u.scheme and u.netloc:
            h["Referer"] = f"{u.scheme}://{u.netloc}/"
    except Exception:
        pass
    r = requests.get(url, headers=h, timeout=12)
    r.raise_for_status()
    return r


def _extract_main_html(html_text: str, base_url: str) -> tuple[str, list[str], str | None]:
    """返回 (英文正文段落列表拼成的纯文本, 文中图片绝对 URL 列表, og:image)。"""
    soup = BeautifulSoup(html_text, "lxml")

    # 拿 og:image 当主图候选
    og_img = None
    og = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "twitter:image"})
    if og and og.get("content"):
        og_img = urljoin(base_url, og["content"].strip())

    # 找正文容器
    candidates = []
    for tag in soup.find_all(["article", "main", "div"]):
        cls = " ".join(tag.get("class", []))
        if any(k in cls for k in ["entry-content", "post-content", "article-content", "td-post-content", "rich_media_content", "content-area"]):
            candidates.append(tag)
    container = candidates[0] if candidates else (soup.find("article") or soup.find("main") or soup.body)
    if container is None:
        return "", [], og_img

    paragraphs: list[str] = []
    images: list[str] = []
    for el in container.descendants:
        name = getattr(el, "name", None)
        if name == "p":
            t = " ".join(el.get_text(" ").split()).strip()
            if t and len(t) > 20:
                paragraphs.append(t)
        elif name == "img":
            src = el.get("src") or el.get("data-src") or el.get("data-lazy-src")
            if src:
                images.append(urljoin(base_url, src.strip()))

    # 去掉广告/订阅类段落
    paragraphs = [p for p in paragraphs if not re.search(r"(subscribe|newsletter|sign up|cookie)", p, re.I)]

    return "\n\n".join(paragraphs[:25]), images[:10], og_img


# ---------- 批量翻译 ----------

_TRANSLATE_SYS = (
    "你是专业的航天科技译者。请把用户提交的若干篇英文新闻翻译成自然、流畅、专业准确的简体中文，"
    "保持段落结构。每篇之间用一个特殊分隔符 `" + ARTICLE_DELIM + "` 分开，翻译结果中也必须用"
    "同样的分隔符把每篇隔开，且数量与输入完全相同。除翻译外不要输出任何额外说明。"
)


def _batch_translate(en_blocks: list[str]) -> list[str]:
    """一次 API 调用翻译多篇，按分隔符切回。"""
    if not en_blocks:
        return []
    joined = ("\n\n" + ARTICLE_DELIM + "\n\n").join(en_blocks)
    try:
        resp = openai_client().chat.completions.create(
            model=SETTINGS.openai_model,
            messages=[
                {"role": "system", "content": _TRANSLATE_SYS},
                {"role": "user", "content": joined},
            ],
            temperature=0.2,
        )
        text = resp.choices[0].message.content
    except Exception as e:
        log.exception("batch translate failed: %s", e)
        return en_blocks  # 翻译失败就降级回原文

    out = [p.strip() for p in text.split(ARTICLE_DELIM)]
    if len(out) != len(en_blocks):
        log.warning("translate split mismatch %d vs %d, fallback", len(out), len(en_blocks))
        # 简单兜底：尝试按双换行 + 数量截
        if len(out) > len(en_blocks):
            out = out[: len(en_blocks)]
        else:
            out = out + en_blocks[len(out):]
    return out


# ---------- HTML 渲染 ----------

_PAGE_TPL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_zh}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
        max-width: 760px; margin: 0 auto; padding: 24px 18px 60px; color: #1f2329; line-height: 1.75; }}
h1 {{ font-size: 22px; line-height: 1.4; margin: 0 0 6px; }}
.meta {{ color: #8a8f99; font-size: 13px; margin-bottom: 18px; }}
.meta a {{ color: #1664ff; text-decoration: none; }}
.hero {{ width: 100%; border-radius: 8px; margin: 12px 0 20px; }}
.body p {{ margin: 0 0 16px; font-size: 16px; }}
.body img {{ max-width: 100%; height: auto; border-radius: 6px; margin: 14px 0; }}
.footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #eee;
           font-size: 13px; color: #8a8f99; }}
.footer a {{ color: #1664ff; word-break: break-all; }}
</style>
</head>
<body>
<h1>{title_zh}</h1>
<div class="meta">来源：{source} · {published}</div>
{hero_html}
<div class="body">
{body_html}
</div>
<div class="footer">
原文链接（英文）：<a href="{orig_url}" target="_blank" rel="noopener">{orig_url}</a>
</div>
</body>
</html>
"""


# 这些站对外部 Referer 严格防盗链：直接 <img src> 会 403
_HOTLINK_BLOCK_HOSTS = (
    "nasaspaceflight.com",
    "www.nasaspaceflight.com",
)


def _proxy_image(url: str) -> str:
    """对盗链严格的来源走 weserv.nl 公共图片代理（服务端拉图、转发，无 Referer 限制）。"""
    if not url:
        return url
    try:
        from urllib.parse import urlparse, quote as _q
        host = urlparse(url).netloc.lower()
        if any(host == h or host.endswith("." + h) for h in _HOTLINK_BLOCK_HOSTS):
            # weserv 要求去掉协议头
            stripped = url.split("://", 1)[-1]
            return f"https://images.weserv.nl/?url={_q(stripped, safe='')}"
    except Exception:
        pass
    return url


def _slugify(text: str, fallback: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:48] or fallback


def _para_to_html(text: str) -> str:
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    return "\n".join(f"<p>{html.escape(p)}</p>" for p in paras)


# ---------- Manifest 轮转 ----------

def _load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text("utf-8"))
        except Exception:
            pass
    return {"batches": []}


def _save_manifest(m: dict) -> None:
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def _rotate(batch_id: str, ids_in_batch: list[str]) -> None:
    m = _load_manifest()
    # 去掉同 id 的旧记录
    m["batches"] = [b for b in m["batches"] if b.get("id") != batch_id]
    m["batches"].append({"id": batch_id, "created_at": int(time.time()), "page_ids": ids_in_batch})
    # 仅保留最近 KEEP_BATCHES 个
    while len(m["batches"]) > KEEP_BATCHES:
        old = m["batches"].pop(0)
        d = PAGES_ROOT / old["id"]
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            log.info("rotated old batch %s", old["id"])
    _save_manifest(m)


# ---------- 主流程 ----------

@dataclass
class PageResult:
    orig_url: str
    page_id: str
    page_path: str  # 相对 /news/ 的 path（带 batch_id）
    image_url: str  # 选定的主图（绝对 URL）


def prepare_news_pages(articles: list[dict], batch_id: str, public_base: str | None = None) -> dict[str, PageResult]:
    """给定 articles（每个 dict 至少有 title/link/image_url/source/published），
    生成翻译页，返回 {orig_url: PageResult}。"""
    out: dict[str, PageResult] = {}
    if not articles:
        return out

    batch_dir = PAGES_ROOT / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    # ----- 1. 抓原文 -----
    fetched: list[dict] = []
    for idx, a in enumerate(articles, 1):
        url = a.get("link", "")
        item = {**a, "_idx": idx, "_text": "", "_imgs": [], "_og": None, "_blocked_kind": ""}

        # 0) 远端 ingest 已带回 content_html → 直接用，绝不再回源
        ingest_html = a.get("content_html") or ""
        if ingest_html:
            text, imgs, og = _extract_main_html(ingest_html, url or "")
            item["_text"] = text
            item["_imgs"] = imgs
            item["_og"] = og
            log.info("use ingest content_html for %s (%d chars text)", url, len(text))
            fetched.append(item)
            continue

        if not url:
            fetched.append(item)
            continue
        try:
            r = _http_get(url)
            text, imgs, og = _extract_main_html(r.text, url)
            item["_text"] = text
            item["_imgs"] = imgs
            item["_og"] = og
            # 即便 200，也可能是 Cloudflare 的 "Just a moment..." 拦截页
            if not text:
                low = (r.text or "").lower()
                if any(k in low for k in ("just a moment", "cf-chl-", "challenge-platform", "attention required")):
                    item["_blocked_kind"] = "cloudflare"
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 403:
                # 403 多为 Cloudflare/WAF 直接拒绝爬虫
                item["_blocked_kind"] = "cloudflare"
            elif status in (429, 451, 503, 520, 521, 522, 523, 524):
                # 429/5xx/451 多为 CDN 限流或地理拦截，不是 Cloudflare 校验
                item["_blocked_kind"] = "ratelimit"
            log.info("fetch %s failed (status=%s): %s", url, status, e)
        fetched.append(item)

    # ----- 2. 批量翻译 -----
    en_blocks: list[str] = []
    en_index: list[int] = []  # fetched 中具有正文的索引
    for i, item in enumerate(fetched):
        txt = item["_text"]
        if txt:
            en_blocks.append(f"TITLE: {item.get('title','')}\n\n{txt}")
            en_index.append(i)

    if en_blocks:
        log.info("translate %d articles in one GPT call", len(en_blocks))
        zh_blocks = _batch_translate(en_blocks)
        for i, zh in zip(en_index, zh_blocks):
            fetched[i]["_zh"] = zh
    for item in fetched:
        item.setdefault("_zh", "")

    # ----- 3. 渲染页面 -----
    ids_in_batch: list[str] = []
    for idx, item in enumerate(fetched, 1):
        slug = _slugify(item.get("title", ""), fallback=f"item-{idx}")
        page_id = f"{idx:02d}-{slug}"
        ids_in_batch.append(page_id)

        zh_text = item.get("_zh") or ""
        # 翻译里把 "TITLE: 中文标题" 拆出来
        title_zh = item.get("title", "")
        if zh_text.startswith("TITLE:") or zh_text.startswith("标题：") or zh_text.startswith("标题:"):
            first_nl = zh_text.find("\n")
            if first_nl > 0:
                title_zh = zh_text[: first_nl].split(":", 1)[-1].split("：", 1)[-1].strip() or title_zh
                zh_text = zh_text[first_nl + 1:].lstrip("\n")

        if zh_text:
            body_html = _para_to_html(zh_text)
        else:
            kind = item.get("_blocked_kind") or ""
            if kind == "cloudflare":
                tip = (
                    "<b>原文暂无法展示</b><br>"
                    "该来源站启用了 <b>Cloudflare 自动程序校验</b>（Bot Challenge），"
                    "我们的服务器请求未能通过人机验证，因此无法渲染中文译文。"
                    "<br>请点击下方“原文链接”在浏览器中直接打开查看英文原版。"
                )
            elif kind == "ratelimit":
                tip = (
                    "<b>原文暂无法展示</b><br>"
                    "该来源站对我们服务器所在的网络段做了访问限流 / 区域拦截"
                    "（HTTP 429/451/5xx），并非 Cloudflare 人机校验。"
                    "<br>我们已通过 GitHub Actions 海外节点尝试抓取全文，"
                    "若本次仍未拿到全文请稍后再试，或点击下方原文链接查看。"
                )
            else:
                tip = (
                    "<b>未能抓取到原文正文</b><br>"
                    "请直接访问下方原文链接查看英文版本。"
                )
            body_html = (
                '<div style="background:#fff7e6;border:1px solid #ffd591;'
                'border-radius:6px;padding:14px 16px;color:#8c4a00;'
                'font-size:15px;line-height:1.7;">'
                + tip + "</div>"
            )

        hero = item.get("image_url") or item.get("_og") or (item["_imgs"][0] if item["_imgs"] else "")
        hero_display = _proxy_image(hero)
        hero_html = f'<img class="hero" src="{html.escape(hero_display)}" alt="">' if hero else ""

        page_html = _PAGE_TPL.format(
            title_zh=html.escape(title_zh),
            source=html.escape(item.get("source", "")),
            published=html.escape(item.get("published", "")),
            hero_html=hero_html,
            body_html=body_html,
            orig_url=html.escape(item.get("link", "")),
        )
        (batch_dir / f"{page_id}.html").write_text(page_html, encoding="utf-8")

        page_path = f"{batch_id}/{page_id}"
        out[item["link"]] = PageResult(
            orig_url=item["link"],
            page_id=page_id,
            page_path=page_path,
            image_url=hero,
        )

    # ----- 4. 轮转 -----
    _rotate(batch_id, ids_in_batch)
    log.info("batch %s wrote %d pages", batch_id, len(ids_in_batch))
    return out


def page_file(batch_id: str, page_id: str) -> Path:
    return PAGES_ROOT / batch_id / f"{page_id}.html"


def latest_batches() -> list[str]:
    m = _load_manifest()
    return [b["id"] for b in m["batches"]]
