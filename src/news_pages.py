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
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

from .config import SETTINGS
from .summarizer import client as openai_client, summarize_zh
from . import tagging

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
_CN_DT_RE = re.compile(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2}):(\d{2})(?::(\d{2}))?")


def _parse_dt(s: str) -> datetime | None:
    """尽量把各种时间字符串解析为带时区的 datetime。"""
    if not s:
        return None
    s = s.strip()
    # 1) ISO 8601（含 +00:00 / Z）
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    # 2) RFC822（Thu, 11 Jun 2026 21:14:44 +0000）
    try:
        d = parsedate_to_datetime(s)
        if d is not None:
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    # 3) 中文 2026年06月11日 09:30:02（spacelive，本身即北京时间）
    m = _CN_DT_RE.search(s)
    if m:
        y, mo, d_, h, mi, se = (int(x) if x else 0 for x in m.groups())
        try:
            return datetime(y, mo, d_, h, mi, se, tzinfo=CST)
        except Exception:
            return None
    return None


def to_beijing(s: str, *, with_label: bool = True) -> str:
    """把时间字符串转为北京时间显示；无法解析则原样返回。"""
    d = _parse_dt(s)
    if not d:
        return s or ""
    out = d.astimezone(CST).strftime("%Y-%m-%d %H:%M")
    return f"{out}（北京时间）" if with_label else out


# 正文内嵌 UTC / 协调世界时 → 北京时间（+8）。
# 括号形式优先，避免出现「（22:49（北京时间））」双层括号；UTC 后可能紧跟中文，不用 \b。
_UTC_PAREN_DATE = re.compile(
    r"[（(]\s*协调世界时\s*(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[:：](\d{2})(?::(\d{2}))?\s*[）)]"
)
_UTC_PAREN_TIME = re.compile(
    r"[（(]\s*协调世界时\s*(\d{1,2})[:：](\d{2})(?::(\d{2}))?\s*[）)]"
)
_UTC_DATE_CN = re.compile(
    r"协调世界时\s*(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[:：](\d{2})(?::(\d{2}))?"
)
_UTC_TIME_CN = re.compile(
    r"(协调世界时|世界时)\s*(\d{1,2})[:：](\d{2})(?::(\d{2}))?"
)
_UTC_TIME_EN = re.compile(
    r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(UTC|GMT)(?![A-Za-z])",
    re.IGNORECASE,
)
_UTC_MARKER = re.compile(r"UTC|GMT|协调世界时|(?<!北京)世界时", re.IGNORECASE)


def _plus8(h: int, m: int, s: int = 0) -> tuple[int, int, int, int]:
    """返回 (时, 分, 秒, 日偏移)。"""
    total = h * 3600 + m * 60 + s + 8 * 3600
    day = total // 86400
    total %= 86400
    return total // 3600, (total % 3600) // 60, total % 60, day


def _fmt_hm(h: int, m: int, s: int = 0, *, with_sec: bool = False) -> str:
    if with_sec:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}"


def _bj_date_part(mo: int, d: int, day_add: int) -> str:
    try:
        base = datetime(2024, mo, d, tzinfo=timezone.utc)
        new = base + timedelta(days=day_add)
        return f"{new.month}月{new.day}日"
    except Exception:
        return f"{mo}月{d}日" + ("次日" if day_add else "")


def utc_times_to_beijing(text: str) -> str:
    """把正文里的 UTC / 协调世界时自动改写为北京时间。

    例：
      ``14:47 UTC`` → ``22:47（北京时间）``
      ``协调世界时02:50`` → ``10:50（北京时间）``
      ``（协调世界时14:49）`` → ``（北京时间 22:49）``
      ``协调世界时7月22日02:04`` → ``7月22日10:04（北京时间）``
    跨日时补「次日」或推进日期。已标注「北京时间」且无 UTC 字样的片段不会再改。
    """
    if not text or not _UTC_MARKER.search(text):
        return text

    def _repl_paren_date(m: re.Match) -> str:
        mo, d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        sec_s = m.group(5)
        s = int(sec_s) if sec_s else 0
        nh, nm, ns, day_add = _plus8(h, mi, s)
        t = _fmt_hm(nh, nm, ns, with_sec=bool(sec_s))
        return f"（北京时间 {_bj_date_part(mo, d, day_add)}{t}）"

    def _repl_paren_time(m: re.Match) -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        sec_s = m.group(3)
        s = int(sec_s) if sec_s else 0
        nh, nm, ns, day_add = _plus8(h, mi, s)
        t = _fmt_hm(nh, nm, ns, with_sec=bool(sec_s))
        prefix = "次日 " if day_add else ""
        return f"（北京时间 {prefix}{t}）"

    def _repl_date(m: re.Match) -> str:
        mo, d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        sec_s = m.group(5)
        s = int(sec_s) if sec_s else 0
        nh, nm, ns, day_add = _plus8(h, mi, s)
        t = _fmt_hm(nh, nm, ns, with_sec=bool(sec_s))
        return f"{_bj_date_part(mo, d, day_add)}{t}（北京时间）"

    def _repl_cn_time(m: re.Match) -> str:
        h, mi = int(m.group(2)), int(m.group(3))
        sec_s = m.group(4)
        s = int(sec_s) if sec_s else 0
        nh, nm, ns, day_add = _plus8(h, mi, s)
        t = _fmt_hm(nh, nm, ns, with_sec=bool(sec_s))
        prefix = "次日" if day_add else ""
        return f"{prefix}{t}（北京时间）"

    def _repl_en_time(m: re.Match) -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        sec_s = m.group(3)
        s = int(sec_s) if sec_s else 0
        nh, nm, ns, day_add = _plus8(h, mi, s)
        t = _fmt_hm(nh, nm, ns, with_sec=bool(sec_s))
        prefix = "次日" if day_add else ""
        return f"{prefix}{t}（北京时间）"

    out = _UTC_PAREN_DATE.sub(_repl_paren_date, text)
    out = _UTC_PAREN_TIME.sub(_repl_paren_time, out)
    out = _UTC_DATE_CN.sub(_repl_date, out)
    out = _UTC_TIME_CN.sub(_repl_cn_time, out)
    out = _UTC_TIME_EN.sub(_repl_en_time, out)
    return out

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
    "你是专业的航天科技译者。请把用户提交的若干篇英文新闻**完整地**翻译成自然、流畅、"
    "专业准确的简体中文。\n"
    "**硬性要求**：\n"
    "1. 每一段原文都必须翻译，不得跳过、合并、概括或省略；译文段落数与原文段落数必须一致。\n"
    "2. 严格保留段落分隔（即译文段落之间也用空行隔开）。\n"
    "3. 每篇英文前都有一个形如 `###@@@SEG<数字>@@@###` 的分隔标记。"
    "请在输出中，**每篇译文前都原样保留它自己的那个标记**（数字与先后顺序都不能变，"
    "不得新增、删除、改写、翻译或合并这些标记）；标记之外不要再输出其它标记。\n"
    "4. 除翻译正文外，不要输出任何额外说明、总结、声明、Markdown 元信息或英文备注。\n"
    "5. 正文中的 UTC / GMT / Coordinated Universal Time / 协调世界时 时刻，请换算成**北京时间（UTC+8）**写出，"
    "并标注「北京时间」；不要只保留 UTC 原时刻。\n"
    "6. 遇到段落是"
    "『 By submitting this form, you agree to ... 』『 Sign up for our newsletter 』『 Subscribe / Sign In 』"
    "等明显是订阅广告 / 服务条款 / Cookie 提示的内容，可以直接丢弃；正文之外的真实段落不得丢。\n"
    "**专有名词对照表**（遇到下列原文必须按此中文译法，不得改写）：\n"
    "- Golden Dome / Golden Dome for America / Golden Dome missile defense → **金穹计划**\n"
    "  （这是 2025 年美国发布的本土反导工程命名，中文官方/媒体一致译作『金穹』，"
    "  不要写成『金顶』『金顶计划』『金圆顶』等。）\n"
    "- Iron Dome（以色列防御系统）→ 铁穹\n"
    "- Space Force / U.S. Space Force → 美国太空军\n"
    "- Space Development Agency (SDA) → 美国太空发展局\n"
    "- Space Rapid Capabilities Office (Space RCO) → 太空快速能力办公室\n"
    "- Artemis（NASA 月球计划）→ 阿尔忒弥斯\n"
    "- Starship → 星舰；Falcon 9 → 猎鹰 9；Starlink → 星链\n"
    "- Starfall → **星落**（专有名，不要译成『落星』『坠星』『流星』等）\n"
    "- LEO / GEO / MEO → 低轨 / 地球同步轨道 / 中地球轨道"
)


# 带编号的分段标记：即使模型漏掉/合并个别标记，也能按编号把对得上的篇目回收，
# 只对「真正缺失/明显截断」的篇目单篇重译，避免整批回退逐篇（既慢又贵）。
_SEG_RE = re.compile(r"#{2,}@@@SEG(\d+)@@@#{2,}")
_BATCH_CHUNK = 6  # 每次 GPT 调用最多翻译多少篇（篇数越少，标记越不易错乱）


def _seg_marker(n: int) -> str:
    return f"###@@@SEG{n}@@@###"


def _translate_group(group: list[str], base: int) -> list[str]:
    """翻译一小批（≤_BATCH_CHUNK 篇），用带编号标记切回，缺失篇目单篇兜底。"""
    joined = "\n\n".join(f"{_seg_marker(base + k + 1)}\n{b}" for k, b in enumerate(group))
    try:
        resp = openai_client().chat.completions.create(
            model=SETTINGS.openai_model,
            messages=[
                {"role": "system", "content": _TRANSLATE_SYS},
                {"role": "user", "content": joined},
            ],
            temperature=0.2,
            max_tokens=8192,
        )
        text = resp.choices[0].message.content or ""
    except Exception as e:
        log.exception("group translate failed: %s", e)
        return [_translate_single(b) for b in group]

    # 按编号回收：marker n → 其后到下一 marker 之间的文本
    found: dict[int, str] = {}
    matches = list(_SEG_RE.finditer(text))
    for i, m in enumerate(matches):
        s = m.end()
        e = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        found[int(m.group(1))] = text[s:e].strip()

    result: list[str] = []
    for k, en in enumerate(group):
        zh = found.get(base + k + 1, "")
        if not zh or len(zh) < max(120, len(en) * 0.25):
            log.warning("seg %d missing/short, retry per-article", base + k + 1)
            zh = _translate_single(en)
        result.append(zh)
    return result


def _batch_translate(en_blocks: list[str]) -> list[str]:
    """分小批翻译多篇；每批用带编号标记切回，缺失篇目单篇兜底。"""
    if not en_blocks:
        return []
    out: list[str] = []
    for start in range(0, len(en_blocks), _BATCH_CHUNK):
        group = en_blocks[start:start + _BATCH_CHUNK]
        log.info("translate group [%d..%d) (%d articles)", start, start + len(group), len(group))
        out.extend(_translate_group(group, base=start))
    return out


def _translate_single(en: str) -> str:
    """单篇翻译，作为批量失败 / 截断时的兜底。"""
    try:
        resp = openai_client().chat.completions.create(
            model=SETTINGS.openai_model,
            messages=[
                {"role": "system", "content": _TRANSLATE_SYS},
                {"role": "user", "content": en},
            ],
            temperature=0.2,
            max_tokens=4096,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.exception("single translate failed: %s", e)
        return en


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
.tags {{ margin: 0 0 14px; }}
.tags span {{ display: inline-block; background: #eef2ff; color: #1664ff; font-size: 13px;
              padding: 2px 10px; border-radius: 12px; margin: 0 6px 6px 0; }}
.hero {{ width: 100%; border-radius: 8px; margin: 12px 0 20px; }}
.blurb {{ background: #f5f8ff; border: 1px solid #e6eeff; border-radius: 8px;
          padding: 14px 16px; margin: 0 0 22px; }}
.blurb .label {{ font-size: 13px; color: #1664ff; font-weight: 600; margin: 0 0 8px; }}
.blurb .txt {{ font-size: 15px; line-height: 1.75; color: #2a3344; margin: 0; text-indent: 0; }}
.body p {{ margin: 0 0 18px; font-size: 16px; text-indent: 2em; line-height: 1.9; }}
.body img {{ max-width: 100%; height: auto; border-radius: 6px; margin: 14px 0; }}
.footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #eee;
           font-size: 13px; color: #8a8f99; }}
.footer a {{ color: #1664ff; word-break: break-all; }}
</style>
</head>
<body>
<h1>{title_zh}</h1>
<div class="meta">来源：{source} · {published}</div>
{tags_html}
{hero_html}
{summary_html}
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


_BIO_PAT = re.compile(
    r"(更多[\s\u4e00-\u9fa5A-Za-z·\.\-]{1,40}作品|"
    r"\.\.\.\s*更多.{1,40}作品|"
    r"…+\s*更多.{1,40}作品)"
)
_BIO_HINT = re.compile(r"(报道|记者|编辑|曾任|供职|博士|硕士|学士|毕业于|涉及|涵盖)")


def _strip_author_bio(text: str) -> str:
    """剥离 SpaceNews 风格的作者署名段（『XX 报道……更多 XX 作品』）。

    译文末尾常出现 1~2 段记者介绍，对正文阅读无意义，按段尾启发式删掉。
    """
    if not text:
        return text
    paras = [p for p in re.split(r"\n{2,}", text) if p.strip()]
    while paras:
        last = paras[-1]
        if _BIO_PAT.search(last) or (_BIO_HINT.search(last) and len(last) < 220 and ("·" in last or "记者" in last or "编辑" in last)):
            paras.pop()
            continue
        break
    return "\n\n".join(paras).strip()


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

def _summary_html(summary_zh: str) -> str:
    """正文前的「内容概要」块（仅国际要闻翻译页）。"""
    s = (summary_zh or "").strip()
    if not s:
        return ""
    return (
        '<div class="blurb">'
        '<div class="label">内容概要</div>'
        f'<p class="txt">{html.escape(s)}</p>'
        "</div>"
    )


@dataclass
class PageResult:
    orig_url: str
    page_id: str
    page_path: str  # 相对 /news/ 的 path（带 batch_id）
    image_url: str  # 选定的主图（绝对 URL）
    title_zh: str = ""   # 中文标题（若翻译失败 = 原文标题）
    body_zh: str = ""    # 中文正文（纯文本，按段落用 \n\n 分隔；翻译失败为空）
    body_en: str = ""    # 英文原文正文（供英检索 / 对照；抓取失败为空）
    summary_zh: str = ""  # 正文前的短概要（2~4 句）
    tags: list = field(default_factory=list)  # 主题标签 + 范围标签


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
        log.info("translate %d articles (chunked, %d/group)", len(en_blocks), _BATCH_CHUNK)
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

        zh_text = utc_times_to_beijing(_strip_author_bio(zh_text))
        # 英文原文：抓取到的正文（已去 HTML），与中文译文一并落库，支持英文语义检索
        body_en = _strip_author_bio((item.get("_text") or "").strip())

        # 内容概要：有中文正文时再生成；失败则跳过（页面仍可展示全文）
        summary_zh = ""
        if zh_text:
            try:
                summary_zh = utc_times_to_beijing(summarize_zh(title_zh, zh_text))
            except Exception as e:
                log.warning("summarize_zh failed for %s: %s", item.get("link"), e)

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

        # 主题标签（基于中文标题+正文）；范围标签固定为国际新闻（三源均为境外英文源）
        tags = tagging.tags_for(f"{title_zh} {zh_text}", scope="国际新闻")
        tags_html = (
            '<div class="tags">' + "".join(f"<span>#{html.escape(t)}</span>" for t in tags) + "</div>"
            if tags else ""
        )
        title_display = (tagging.tag_prefix(tags) + title_zh).strip()

        hero = item.get("image_url") or item.get("_og") or (item["_imgs"][0] if item["_imgs"] else "")
        hero_display = _proxy_image(hero)
        hero_html = f'<img class="hero" src="{html.escape(hero_display)}" alt="">' if hero else ""

        page_html = _PAGE_TPL.format(
            title_zh=html.escape(title_display),
            source=html.escape(item.get("source", "")),
            published=html.escape(to_beijing(item.get("published", ""))),
            tags_html=tags_html,
            hero_html=hero_html,
            summary_html=_summary_html(summary_zh),
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
            title_zh=title_zh,
            body_zh=zh_text,
            body_en=body_en,
            summary_zh=summary_zh,
            tags=tags,
        )

    # ----- 4. 轮转 -----
    _rotate(batch_id, ids_in_batch)
    log.info("batch %s wrote %d pages", batch_id, len(ids_in_batch))
    return out


def _render_page_html(*, title_zh: str, source: str, published: str,
                      orig_url: str, body_zh: str, image_url: str,
                      summary_zh: str = "") -> str:
    """仅用已有字段渲染一张翻译页 HTML（不抓取、不翻译）。"""
    body_zh = utc_times_to_beijing(body_zh or "")
    if body_zh.strip():
        body_html = _para_to_html(body_zh)
    else:
        body_html = (
            '<div style="background:#fff7e6;border:1px solid #ffd591;'
            'border-radius:6px;padding:14px 16px;color:#8c4a00;'
            'font-size:15px;line-height:1.7;">'
            "<b>原文暂无法展示</b><br>请点击下方“原文链接”查看英文原版。</div>"
        )
    tags = tagging.tags_for(f"{title_zh} {body_zh}", scope="国际新闻")
    tags_html = (
        '<div class="tags">' + "".join(f"<span>#{html.escape(t)}</span>" for t in tags) + "</div>"
        if tags else ""
    )
    title_display = (tagging.tag_prefix(tags) + (title_zh or "")).strip()
    hero_display = _proxy_image(image_url or "")
    hero_html = f'<img class="hero" src="{html.escape(hero_display)}" alt="">' if image_url else ""
    return _PAGE_TPL.format(
        title_zh=html.escape(title_display),
        source=html.escape(source or ""),
        published=html.escape(to_beijing(published or "")),
        tags_html=tags_html,
        hero_html=hero_html,
        summary_html=_summary_html(summary_zh),
        body_html=body_html,
        orig_url=html.escape(orig_url or ""),
    )


def rebuild_pages_from_cache(articles: list[dict]) -> int:
    """重发场景：根据缓存里已存的 body_zh/title_zh/image_url 直接把翻译页写回磁盘，
    不重新抓取/翻译。页面路径从每条的 link(/news/{batch}/{page_id}) 解析。

    返回成功重建的页面数。
    """
    n = 0
    for a in articles or []:
        link = a.get("link") or ""
        if "/news/" not in link:
            continue
        try:
            tail = link.split("/news/", 1)[1].strip("/")
            batch_id, page_id = tail.split("/", 1)
        except Exception:
            continue
        if not batch_id or not page_id:
            continue
        try:
            page_html = _render_page_html(
                title_zh=a.get("title_zh") or a.get("title") or "",
                source=a.get("source") or "",
                published=a.get("published") or "",
                orig_url=a.get("original_link") or "",
                body_zh=a.get("body_zh") or "",
                image_url=a.get("image_url") or "",
                summary_zh=a.get("summary_zh") or "",
            )
            d = PAGES_ROOT / batch_id
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{page_id}.html").write_text(page_html, encoding="utf-8")
            n += 1
        except Exception as e:
            log.warning("rebuild page failed for %s: %s", link, e)
    log.info("rebuild_pages_from_cache wrote %d pages", n)
    return n


def page_file(batch_id: str, page_id: str) -> Path:
    return PAGES_ROOT / batch_id / f"{page_id}.html"


def latest_batches() -> list[str]:
    m = _load_manifest()
    return [b["id"] for b in m["batches"]]
