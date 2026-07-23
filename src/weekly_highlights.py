"""把已有航天资讯打包成每周 highlights 合辑，并创建客户群发待办。"""
from __future__ import annotations

import html
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

from .config import ROOT, SETTINGS
from .img_proxy import public_base
from .wecom_external import create_attachment_mass_task, list_external_userids

log = logging.getLogger(__name__)

HIGHLIGHTS_DIR = ROOT / "data" / "highlights"
KIND_LABELS = {
    "intl": "国际要闻",
    "gzh": "公众号精选",
    "launch": "发射动态",
    "future": "未来发射",
    "techport": "前沿技术",
    "debris": "空间环境",
    "douyin": "航天视频",
}
def _now() -> datetime:
    return datetime.now(ZoneInfo(SETTINGS.daily_tz))


def current_week_id(now: datetime | None = None) -> str:
    date = (now or _now()).date()
    iso = date.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _week_dir(week_id: str) -> Path:
    if not (
        len(week_id) == 8
        and week_id[:4].isdigit()
        and week_id[4:6] == "-W"
        and week_id[6:].isdigit()
    ):
        raise ValueError("week_id 格式应为 YYYY-Www")
    return HIGHLIGHTS_DIR / week_id


def page_file(week_id: str) -> Path:
    return _week_dir(week_id) / "index.html"


def cover_file(week_id: str) -> Path:
    return _week_dir(week_id) / "cover.png"


def item_page_file(week_id: str, item_id: str) -> Path:
    if not item_id or not all(c.isalnum() or c in "-_" for c in item_id):
        raise ValueError("bad item id")
    return _week_dir(week_id) / "items" / f"{item_id}.html"


def card_image_file(week_id: str, item_id: str) -> Path:
    if not item_id or not all(c.isalnum() or c in "-_" for c in item_id):
        raise ValueError("bad item id")
    return _week_dir(week_id) / "cards" / f"{item_id}.jpg"


def manifest_file(week_id: str) -> Path:
    return _week_dir(week_id) / "manifest.json"


def _load_items(days: int = 7) -> list[dict]:
    workspace = ROOT.parent
    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))
    from weixin_miniprogram.backend import news_store

    selected: list[dict] = []
    seen: set[str] = set()
    for kind in KIND_LABELS:
        payload = news_store.week(days=days, kind=kind, limit=0)
        for card in payload.get("items") or []:
            item = news_store.detail(card.get("id")) or card
            key = item.get("id") or item.get("link") or item.get("title")
            if key and key not in seen:
                seen.add(key)
                selected.append(item)
    selected.sort(key=lambda item: item.get("published", ""), reverse=True)
    max_items = int(os.getenv("WEEKLY_MAX_ITEMS", "0"))
    return selected[:max_items] if max_items > 0 else selected


def _summary(items: list[dict]) -> str:
    if not items:
        return "本周暂无可用的航天资讯，合辑已生成，等待后续数据更新。"
    topics = [str(item.get("title") or "").strip() for item in items[:3]]
    topics = [topic for topic in topics if topic]
    lead = "、".join(topics)
    if len(lead) > 120:
        lead = lead[:119] + "…"
    return f"本周精选 {len(items)} 条航天动态，重点关注：{lead}"


def search_weekly(
    week_id: str,
    query: str,
    *,
    scope: str = "all",
    sort: str = "score",
    limit: int = 50,
) -> dict:
    """复用小程序的中英互译、关键词与 embedding，在本周合辑内搜索。"""
    manifest = json.loads(manifest_file(week_id).read_text("utf-8"))
    items = manifest.get("items") or []
    query = (query or "").strip()
    scope = scope if scope in ("all", "title") else "all"
    sort = sort if sort in ("time", "score") else "score"
    limit = max(1, min(int(limit), 100))
    if not query:
        return {"q": "", "q_alt": "", "total": 0, "items": [], "mode": "empty"}

    workspace = ROOT.parent
    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))
    from weixin_miniprogram.backend import search_store

    query_alt = search_store._translate_query_bilingual(query)
    queries = [query] + ([query_alt] if query_alt else [])
    lexical = search_store._lexical_match(items, queries, scope)

    query_vectors = []
    for value in queries:
        vector = search_store._embed_query(value, scope)
        if vector is not None:
            query_vectors.append(vector)
    semantic: dict[str, float] = {}
    if query_vectors:
        vectors = search_store._ensure_embeddings(items, scope=scope)
        for item in items:
            vector = vectors.get(item.get("id"))
            if not vector:
                continue
            similarity = max(search_store._cosine(qv, vector) for qv in query_vectors)
            if similarity >= search_store._SEM_MIN:
                semantic[item["id"]] = similarity

    by_id = {item["id"]: item for item in items if item.get("id")}
    lexical_max = max(lexical.values()) if lexical else 1.0
    ranked: list[tuple[float, dict]] = []
    seen: set[str] = set()
    for item_id, similarity in semantic.items():
        item = by_id.get(item_id)
        if not item:
            continue
        lexical_score = lexical.get(item_id, 0.0)
        score = 0.75 * similarity + 0.25 * (lexical_score / lexical_max)
        if lexical_score >= 5.0:
            score += 0.08
        if score >= search_store._FINAL_MIN or lexical_score >= 5.0:
            ranked.append((score, item))
            seen.add(item_id)
    for item_id, lexical_score in lexical.items():
        if item_id in seen or item_id not in by_id:
            continue
        ranked.append((0.40 + 0.45 * lexical_score / lexical_max, by_id[item_id]))

    if sort == "time":
        ranked.sort(key=lambda pair: pair[1].get("published_ts") or 0, reverse=True)
    else:
        ranked.sort(
            key=lambda pair: (pair[0], pair[1].get("published_ts") or 0),
            reverse=True,
        )
    results = []
    for score, item in ranked[:limit]:
        results.append({
            "id": item.get("id"),
            "kind": item.get("kind"),
            "title": item.get("title"),
            "summary": item.get("summary"),
            "source": item.get("source"),
            "published": item.get("published"),
            "main_tag": item.get("main_tag"),
            "image": item.get("image"),
            "url": item.get("link") if item.get("kind") == "gzh" else item.get("internal_url"),
            "score": round(float(score), 4),
        })
    return {
        "q": query,
        "q_alt": query_alt,
        "scope": scope,
        "sort": sort,
        "mode": "semantic" if query_vectors else "bilingual_lexical",
        "total": len(ranked),
        "items": results,
    }


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _make_cover(path: Path, week_label: str, item_count: int) -> None:
    width, height = 1200, 630
    image = Image.new("RGB", (width, height), "#07162f")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / height
        draw.line((0, y, width, y), fill=(7 + int(12 * ratio), 22 + int(22 * ratio), 47 + int(40 * ratio)))
    draw.ellipse((820, -160, 1320, 340), fill="#163d73")
    draw.ellipse((930, -70, 1240, 240), outline="#4d8bd6", width=3)
    draw.text((82, 92), "SPACE HIGHLIGHTS", font=_font(34, True), fill="#75b8ff")
    draw.text((82, 190), "WEEKLY SPACE BRIEF", font=_font(65, True), fill="white")
    draw.text((82, 330), week_label, font=_font(42), fill="#d6e9ff")
    draw.rounded_rectangle((82, 448, 390, 530), radius=41, fill="#1976d2")
    draw.text((122, 466), f"{item_count} TOP STORIES", font=_font(27, True), fill="white")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)


def _selected_cards(items: list[dict]) -> list[dict]:
    """固定六张精选：4 国际新闻 + 1 TechPort + 1 未来发射；缺项时以其它非公众号内容补足。"""
    selected: list[dict] = []
    selected.extend([item for item in items if item.get("kind") == "intl"][:4])
    selected.extend([item for item in items if item.get("kind") == "techport"][:1])
    selected.extend([item for item in items if item.get("kind") == "future"][:1])
    seen = {item.get("id") for item in selected}
    for item in items:
        if len(selected) >= 6:
            break
        if item.get("kind") != "gzh" and item.get("id") not in seen:
            selected.append(item)
            seen.add(item.get("id"))
    return selected[:6]


def _paragraphs(value: str) -> list[str]:
    import re
    text = html.unescape(re.sub(r"<[^>]+>", "\n", value or ""))
    return [part.strip() for part in re.split(r"\n+", text) if part.strip()]


def _internal_item_url(week_id: str, item: dict) -> str:
    return f"{public_base().rstrip('/')}/highlights/{week_id}/items/{item['id']}"


def _render_detail_page(item: dict, week_id: str) -> str:
    title = html.escape(str(item.get("title") or "航天动态"))
    source = html.escape(str(item.get("source") or "航天信息整理"))
    published = html.escape(str(item.get("published") or ""))
    kind = item.get("kind") or ""
    label = html.escape(KIND_LABELS.get(kind, "航天动态"))
    summary = html.escape(str(item.get("summary_zh") or item.get("summary") or ""))
    image = html.escape(str(item.get("image") or ""), quote=True)
    hero = f'<img class="hero-img" src="{image}" alt="">' if image else ""

    sections: list[str] = []
    if summary:
        sections.append(f'<section class="blurb"><b>内容概要</b><p>{summary}</p></section>')

    if kind == "techport":
        fields = [
            ("项目 ID", item.get("project_id")), ("英文标题", item.get("title_en")),
            ("项目状态", item.get("status_zh")), ("项目周期", item.get("period")),
            ("技术成熟度", item.get("trl")), ("技术类别", item.get("category_disp")),
            ("所属计划", item.get("program_disp")), ("主管司局", item.get("directorate")),
            ("牵头机构", item.get("lead_org")), ("任务目的地", item.get("destinations")),
        ]
        rows = "".join(
            f'<div class="meta-row"><span>{html.escape(key)}</span><strong>{html.escape(str(value))}</strong></div>'
            for key, value in fields if value not in (None, "")
        )
        if rows:
            sections.append(f'<section class="meta-card">{rows}</section>')
    elif kind == "future":
        launches = item.get("launches") or []
        rows = []
        for launch in launches:
            time_text = html.escape(str(launch.get("net_bj") or "时间待定"))
            name = html.escape(str(launch.get("name_zh") or launch.get("name_en") or "未命名任务"))
            provider = html.escape(str(launch.get("provider_zh") or launch.get("provider") or ""))
            location = html.escape(str(launch.get("location_zh") or launch.get("location") or ""))
            rows.append(
                f'<div class="launch"><b>{time_text} <small>北京</small></b>'
                f'<h3>{name}</h3><p>{provider}{" · " if provider and location else ""}{location}</p></div>'
            )
        sections.append(
            f'<section><div class="future-head">未来 {item.get("future_days") or 30} 天'
            f'共 {len(launches)} 次发射</div>{"".join(rows)}</section>'
        )

    body_source = item.get("tp_summary") if kind == "techport" else item.get("body")
    paragraphs = _paragraphs(str(body_source or ""))
    if paragraphs:
        sections.append(
            '<section class="body">' +
            "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs) +
            "</section>"
        )

    original = html.escape(str(item.get("link") or ""), quote=True)
    original_link = (
        f'<a class="original" href="{original}" rel="noopener">查看信息来源</a>' if original else ""
    )
    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f5f7fb;color:#20293a;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
.page{{max-width:760px;margin:auto;background:#fff;min-height:100vh;padding:28px 20px 50px}}.tag{{display:inline-block;padding:5px 11px;border-radius:14px;background:#eaf2ff;color:#1664ff;font-size:12px}}
h1{{font-size:27px;line-height:1.45;margin:14px 0 8px}}.sub{{font-size:13px;color:#8b96a8}}.hero-img{{width:100%;border-radius:12px;margin:22px 0 4px}}
.blurb,.meta-card{{margin-top:20px;padding:18px;background:#f5f8ff;border:1px solid #e6eeff;border-radius:12px}}.blurb b{{font-size:13px;color:#1664ff}}.blurb p{{margin:9px 0 0}}
p{{font-size:16px;line-height:1.85;color:#354055}}.meta-row{{display:flex;padding:11px 0;border-bottom:1px solid #e6edf8;gap:14px}}.meta-row:last-child{{border:0}}.meta-row span{{width:100px;color:#8490a4;font-size:14px}}.meta-row strong{{flex:1;font-size:14px;font-weight:500}}
.future-head{{margin-top:24px;color:#667389}}.launch{{padding:20px 4px;border-bottom:1px solid #e5e9f0}}.launch b{{color:#1f6feb}}.launch small{{font-weight:400;color:#8b96a8}}.launch h3{{font-size:17px;margin:10px 0 5px}}.launch p{{font-size:14px;margin:0;color:#667389}}
.body{{margin-top:22px}}.original{{display:block;margin-top:30px;padding:13px;text-align:center;color:#1f6feb;background:#f5f8ff;border-radius:24px;text-decoration:none}}
</style></head><body><main class="page"><span class="tag">#{label}</span><h1>{title}</h1>
<div class="sub">{source} · {published}</div>{hero}{"".join(sections)}{original_link}</main></body></html>"""


def _make_card_image(item: dict, week_id: str) -> str:
    target = card_image_file(week_id, str(item["id"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    image_url = str(item.get("image") or "")
    if image_url:
        try:
            response = requests.get(image_url, timeout=15)
            response.raise_for_status()
            image = Image.open(BytesIO(response.content)).convert("RGB")
            image.thumbnail((900, 506), Image.LANCZOS)
            canvas = Image.new("RGB", (900, 506), "#0b1f3d")
            left = (900 - image.width) // 2
            top = (506 - image.height) // 2
            canvas.paste(image, (left, top))
            canvas.save(target, "JPEG", quality=84, optimize=True)
            return f"{public_base().rstrip('/')}/highlights/{week_id}/cards/{item['id']}.jpg"
        except Exception as exc:
            log.warning("weekly card image fallback id=%s: %s", item.get("id"), exc)
    return f"{public_base().rstrip('/')}/highlights/{week_id}/cover.png"


def _render_page(title: str, summary: str, period: str, items: list[dict], week_id: str) -> str:
    cards: list[str] = []
    for item in items:
        kind = KIND_LABELS.get(item.get("kind", ""), "航天动态")
        item_title = html.escape(str(item.get("title") or "未命名动态"))
        item_summary = html.escape(str(item.get("summary") or ""))
        source = html.escape(str(item.get("source") or ""))
        published = html.escape(str(item.get("published") or ""))
        link = html.escape(
            str(item.get("link") if item.get("kind") == "gzh" else item.get("internal_url") or "#"),
            quote=True,
        )
        cards.append(
            '<article class="item">'
            f'<div class="meta"><span>{html.escape(kind)}</span>{source} · {published}</div>'
            f'<h2><a href="{link}" rel="noopener">{item_title}</a></h2>'
            f'<p>{item_summary}</p>'
            "</article>"
        )
    body = "\n".join(cards) or '<div class="empty">本周暂无可展示内容</div>'
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#f3f6fa;color:#172033;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
.hero{{padding:44px 20px 40px;background:linear-gradient(135deg,#07162f,#123d70);color:#fff}}
.wrap{{max-width:760px;margin:auto}} .eyebrow{{color:#78baff;font-size:13px;font-weight:700;letter-spacing:2px}}
h1{{font-size:30px;line-height:1.35;margin:12px 0}} .lead{{color:#d7e7fa;line-height:1.75;margin:0}} .period{{color:#9ebbdc;font-size:13px;margin-top:15px}}
.search{{padding:20px 16px 0}}.search-box{{display:flex;align-items:center;background:#fff;border:1px solid #dfe6f1;border-radius:24px;padding:0 15px;box-shadow:0 4px 16px rgba(25,45,80,.06)}}.search-box input{{flex:1;border:0;outline:0;font-size:15px;padding:13px 8px;background:transparent}}.clear{{border:0;background:none;color:#8b96a8;font-size:22px;display:none}}
.search-tools{{display:none;align-items:center;gap:7px;flex-wrap:wrap;margin-top:12px}}.search-tools button{{border:1px solid #dce5f2;background:#fff;color:#637087;border-radius:15px;padding:6px 11px}}.search-tools button.on{{color:#1769aa;background:#eaf4ff;border-color:#bddcff}}.search-status{{margin-left:auto;color:#8491a5;font-size:12px}}
.list{{padding:22px 16px 50px}} .item{{background:#fff;border:1px solid #e6ebf2;border-radius:14px;padding:20px;margin-bottom:14px;box-shadow:0 4px 18px rgba(25,45,80,.05)}}
.meta{{font-size:12px;color:#8491a5}} .meta span{{display:inline-block;color:#1769aa;background:#eaf4ff;border-radius:12px;padding:3px 9px;margin-right:9px}}
h2{{font-size:18px;line-height:1.55;margin:11px 0 7px}} a{{color:#172033;text-decoration:none}} p{{font-size:14px;line-height:1.75;color:#596579;margin:0}}
.empty{{text-align:center;color:#8491a5;padding:70px 0}} .foot{{text-align:center;color:#9ca8b8;font-size:12px;margin-top:24px}}
</style></head><body>
<header class="hero"><div class="wrap"><div class="eyebrow">SPACE HIGHLIGHTS</div>
<h1>{html.escape(title)}</h1><p class="lead">{html.escape(summary)}</p><div class="period">{html.escape(period)}</div>
</div></header>
<section class="search"><div class="wrap"><div class="search-box"><span>🔍</span>
<input id="searchInput" placeholder="中英文模糊搜索标题、正文与来源" autocomplete="off">
<button id="clearSearch" class="clear" aria-label="清除">×</button></div>
<div id="searchTools" class="search-tools">
<button class="scope on" data-value="all">全文</button><button class="scope" data-value="title">标题</button>
<button class="sort on" data-value="score">按匹配度</button><button class="sort" data-value="time">按时间</button>
<span id="searchStatus" class="search-status"></span></div></div></section>
<main class="list"><div class="wrap"><div id="defaultList">{body}</div><div id="searchList"></div>
<div class="foot">航天信息整理 · 每周更新</div></div></main>
<script>
const weekId={json.dumps(week_id)}, input=document.getElementById('searchInput');
const clearBtn=document.getElementById('clearSearch'), tools=document.getElementById('searchTools');
const statusEl=document.getElementById('searchStatus'), defaultList=document.getElementById('defaultList');
const resultList=document.getElementById('searchList'); let timer=null, scope='all', sort='score', seq=0;
function text(el, value){{el.textContent=value==null?'':String(value);return el}}
function render(items){{
  resultList.replaceChildren();
  if(!items.length){{const e=document.createElement('div');e.className='empty';e.textContent='没有找到匹配的内容';resultList.appendChild(e);return}}
  items.forEach(x=>{{
    const card=document.createElement('article');card.className='item';
    const meta=document.createElement('div');meta.className='meta';
    const tag=document.createElement('span');text(tag,x.main_tag||'航天动态');meta.append(tag,document.createTextNode((x.source?' '+x.source:'')+(x.published?' · '+x.published:'')));
    const h=document.createElement('h2'),a=document.createElement('a');a.href=x.url||'#';a.rel='noopener';text(a,x.title);h.appendChild(a);
    const p=document.createElement('p');text(p,x.summary);card.append(meta,h,p);resultList.appendChild(card);
  }});
}}
async function run(){{
  const q=input.value.trim(), current=++seq;
  clearBtn.style.display=q?'block':'none';tools.style.display=q?'flex':'none';
  if(!q){{defaultList.style.display='block';resultList.replaceChildren();statusEl.textContent='';return}}
  defaultList.style.display='none';statusEl.textContent='搜索中…';
  try{{
    const url=`/highlights/${{weekId}}/search?q=${{encodeURIComponent(q)}}&scope=${{scope}}&sort=${{sort}}`;
    const r=await fetch(url);if(!r.ok)throw new Error('HTTP '+r.status);const data=await r.json();
    if(current!==seq)return;render(data.items||[]);statusEl.textContent=`共 ${{data.total||0}} 条${{data.q_alt?' · '+data.q_alt:''}}`;
  }}catch(e){{if(current!==seq)return;resultList.innerHTML='<div class="empty">搜索失败，请稍后重试</div>';statusEl.textContent=''}}
}}
input.addEventListener('input',()=>{{clearTimeout(timer);timer=setTimeout(run,500)}});
input.addEventListener('keydown',e=>{{if(e.key==='Enter'){{clearTimeout(timer);run()}}}});
clearBtn.addEventListener('click',()=>{{input.value='';run();input.focus()}});
document.querySelectorAll('.scope,.sort').forEach(btn=>btn.addEventListener('click',()=>{{
  const cls=btn.classList.contains('scope')?'scope':'sort';
  document.querySelectorAll('.'+cls).forEach(x=>x.classList.remove('on'));btn.classList.add('on');
  if(cls==='scope')scope=btn.dataset.value;else sort=btn.dataset.value;run();
}}));
</script>
</body></html>"""


def prepare_weekly(week_id: str | None = None) -> dict:
    now = _now()
    week_id = week_id or current_week_id(now)
    directory = _week_dir(week_id)
    directory.mkdir(parents=True, exist_ok=True)

    previous: dict = {}
    manifest_path = manifest_file(week_id)
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    items = _load_items(days=7)
    start = (now - timedelta(days=6)).date()
    end = now.date()
    period = f"{start:%Y年%m月%d日}—{end:%m月%d日}"
    month_week = (end.day - 1) // 7 + 1
    title = f"🚀 {end.month}月第{month_week}周航天 Highlights"
    summary = _summary(items)
    base = public_base().rstrip("/")
    page_url = f"{base}/highlights/{week_id}"
    cover_url = f"{base}/highlights/{week_id}/cover.png"

    cover_period = f"{start:%b %d} - {end:%b %d, %Y}".upper()
    _make_cover(cover_file(week_id), cover_period, len(items))
    for item in items:
        if item.get("kind") == "gzh" or not item.get("id"):
            continue
        item["internal_url"] = _internal_item_url(week_id, item)
        target = item_page_file(week_id, str(item["id"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render_detail_page(item, week_id), encoding="utf-8")
    for item in _selected_cards(items):
        item["card_picurl"] = _make_card_image(item, week_id)
    page_file(week_id).write_text(
        _render_page(title, summary, period, items, week_id),
        encoding="utf-8",
    )
    manifest = {
        "week_id": week_id,
        "generated_at": now.isoformat(),
        "title": title,
        "summary": summary,
        "period": period,
        "page_url": page_url,
        "cover_url": cover_url,
        "item_count": len(items),
        "items": items,
    }
    if previous.get("task"):
        manifest["task"] = previous["task"]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("weekly highlights prepared: %s (%d items)", week_id, len(items))
    return manifest


def create_weekly_task(week_id: str | None = None, *, force: bool = False) -> dict:
    manifest = prepare_weekly(week_id)
    old_task = manifest.get("task") or {}
    if old_task.get("msgid") and not force:
        log.info("weekly task already exists, skip: %s", old_task["msgid"])
        return old_task

    raw_ids = os.getenv("WECOM_EXTERNAL_USERIDS", "")
    external_userids = [part.strip() for part in raw_ids.replace("|", ",").split(",") if part.strip()]
    if not external_userids:
        external_userids = list_external_userids()
    if not external_userids:
        raise RuntimeError(f"{SETTINGS.external_sender} 名下没有可群发客户")
    items = manifest.get("items") or []
    attachments: list[dict] = []

    # 固定六张精选卡片：4 国际新闻 + 1 TechPort + 1 未来发射。
    featured = _selected_cards(items)
    # 官方虽允许 9 个附件，但实测满 9 个会长期停在 41063；保守使用已验证可分发的 8 个。
    public_accounts = [item for item in items if item.get("kind") == "gzh" and item.get("link")][:1]
    for item in featured:
        attachments.append({
            "msgtype": "link",
            "link": {
                "title": item.get("title") or "航天动态",
                "desc": item.get("summary") or f"来源：{item.get('source') or '航天信息整理'}",
                "url": item["internal_url"],
                "picurl": item.get("card_picurl") or manifest["cover_url"],
            },
        })
    for item in public_accounts:
        attachments.append({
            "msgtype": "link",
            "link": {
                "title": item.get("title") or "公众号精选",
                "desc": item.get("summary") or f"来源：{item.get('source') or '公众号'}",
                "url": item["link"],
            },
        })

    # 第八张卡片进入包含本周全部条目的合辑网页。
    attachments.append({
        "msgtype": "link",
        "link": {
            "title": f"查看本周全部 {manifest['item_count']} 条航天信息",
            "desc": manifest["summary"],
            "url": manifest["page_url"],
            "picurl": manifest["cover_url"],
        },
    })
    result = create_attachment_mass_task(
        attachments=attachments,
        text="🚀 本周航天 Highlights",
        external_userids=external_userids or None,
    )
    manifest["task"] = {
        "msgid": result.get("msgid", ""),
        "created_at": _now().isoformat(),
        "sender": SETTINGS.external_sender,
        "attachment_count": len(attachments),
        "response": result,
    }
    manifest_file(manifest["week_id"]).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest["task"]
