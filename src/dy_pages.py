"""为单条抖音作品生成一个本机中转页，企业微信卡片点击后：
1. 移动端自动尝试唤起抖音 App（snssdk1128://aweme/detail/?id=...）；
2. 唤起失败/桌面端：显示完整"抖音口令"文本，一键复制，回抖音 App 即弹视频。

页面落盘到 data/dy_pages/<aweme_id>.html，由 server.py 的 /dy/{aweme_id} 路由静态返回。
"""
from __future__ import annotations

import html
import logging
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DY_PAGES_DIR = ROOT / "data" / "dy_pages"
DY_PAGES_DIR.mkdir(parents=True, exist_ok=True)


_PAGE_TPL = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>{title} - 抖音作品</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:16px 14px 32px;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;color:#111;background:#fff;line-height:1.55;max-width:640px;margin:16px auto}}
h3{{font-size:17px;margin:0 0 6px;font-weight:600}}
.meta{{color:#888;font-size:12px;margin:0 0 14px}}
.cover{{width:100%;border-radius:12px;display:block;margin:0 0 14px;background:#eee;aspect-ratio:9/16;object-fit:cover}}
.section{{margin:14px 0 0;padding:14px;border:1px solid #ececec;border-radius:12px;background:#fafafa}}
.section .label{{font-size:12px;color:#666;margin:0 0 8px;line-height:1.5}}
.section .value{{font-family:ui-monospace,Menlo,Consolas,"PingFang SC",sans-serif;font-size:13px;color:#222;word-break:break-all;white-space:pre-wrap;line-height:1.7;-webkit-user-select:text;user-select:text}}
.hint{{font-size:12px;color:#888;margin:12px 2px 0;line-height:1.5}}
</style>
</head><body>
<h3>{title}</h3>
<p class="meta">{source} · 发布于 {published}</p>
{cover_html}

<div class="section">
  <div class="label">🌐 浏览器访问链接</div>
  <div class="value">{share_url_text}</div>
</div>

<div class="section">
  <div class="label">📨 抖音口令（在抖音 App 搜索框粘贴即可播放）</div>
  <div class="value">{share_text}</div>
</div>

<p class="hint">提示：在本页面长按上方文本即可整段选中并复制。</p>
</body></html>
"""


def render_landing(
    aweme_id: str,
    *,
    title: str,
    source: str,
    published: str,
    share_text: str,
    share_url: str,
    image_proxy_url: str = "",
) -> Path:
    """生成/覆盖一个抖音作品的中转页，返回文件路径。

    share_text: 完整口令文本（含 URL）
    image_proxy_url: 已经走 /img 代理的封面 URL（可空）
    """
    cover_html = (
        f'<img class="cover" src="{html.escape(image_proxy_url)}" alt="cover">'
        if image_proxy_url else ""
    )
    page = _PAGE_TPL.format(
        title=html.escape(title or "抖音作品"),
        source=html.escape(source or ""),
        published=html.escape(published or ""),
        cover_html=cover_html,
        share_text=html.escape(share_text or "（该条作品无口令文本，请直接在抖音 App 内搜索作者）"),
        share_url_text=html.escape(share_url),
        aweme_id=html.escape(aweme_id),
    )
    out = DY_PAGES_DIR / f"{aweme_id}.html"
    out.write_text(page, encoding="utf-8")
    return out


def page_file(aweme_id: str) -> Path:
    return DY_PAGES_DIR / f"{aweme_id}.html"
