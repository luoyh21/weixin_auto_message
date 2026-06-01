"""为单条抖音作品生成一个本机中转页（企业微信卡片点击的目标）。

页面长这样：
- 顶部封面 + 标题 + 来源/发布时间
- 两个大按钮：「复制浏览器链接」「复制抖音口令」
- 按钮下方各有一块灰底文本块，作为复制失败时的人工兜底（可长按选中）
- 点按钮时尝试 navigator.clipboard → 失败回退到 textarea + execCommand('copy')，
  无论成功失败都弹一个解释怎么用的 modal（成功告诉怎么用，失败告诉怎么手动复制）

页面落盘到 data/dy_pages/<aweme_id>.html，由 server.py 的 /dy/{aweme_id} 路由静态返回。
"""
from __future__ import annotations

import html
import json
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
body{{margin:0;padding:16px 14px 40px;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;color:#111;background:#fff;line-height:1.55;max-width:640px;margin:16px auto}}
h3{{font-size:18px;margin:0 0 6px;font-weight:600;line-height:1.4}}
.meta{{color:#888;font-size:12px;margin:0 0 14px}}
.cover{{width:100%;border-radius:12px;display:block;margin:0 0 14px;background:#eee;aspect-ratio:9/16;object-fit:cover}}
.btn{{display:block;width:100%;text-align:center;padding:14px 16px;margin:10px 0 0;border:0;border-radius:24px;font-size:15px;font-weight:600;cursor:pointer;letter-spacing:.5px;box-shadow:0 4px 14px rgba(255,0,80,.18)}}
.btn.primary{{background:linear-gradient(90deg,#ff0050,#ff4d6a);color:#fff}}
.btn.secondary{{background:#fff;color:#ff0050;border:1.5px solid #ff0050;box-shadow:none}}
.btn:active{{transform:scale(.985)}}
.section{{margin:14px 0 0;padding:12px 14px;border:1px solid #ececec;border-radius:12px;background:#fafafa}}
.section .label{{font-size:12px;color:#666;margin:0 0 8px;line-height:1.5}}
.section .value{{font-family:ui-monospace,Menlo,Consolas,"PingFang SC",sans-serif;font-size:13px;color:#222;word-break:break-all;white-space:pre-wrap;line-height:1.7;-webkit-user-select:text;user-select:text}}

/* Modal */
.mask{{position:fixed;inset:0;background:rgba(0,0,0,.42);display:none;align-items:center;justify-content:center;z-index:99;padding:24px}}
.mask.show{{display:flex}}
.dlg{{background:#fff;border-radius:14px;max-width:340px;width:100%;padding:20px 20px 14px;box-shadow:0 12px 36px rgba(0,0,0,.18);text-align:center}}
.dlg .icon{{width:48px;height:48px;border-radius:50%;background:linear-gradient(135deg,#ff0050,#ff4d6a);color:#fff;display:flex;align-items:center;justify-content:center;font-size:24px;margin:0 auto 10px}}
.dlg .title{{font-size:16px;font-weight:600;margin:0 0 8px}}
.dlg .msg{{font-size:14px;color:#444;line-height:1.6;margin:0 0 14px;text-align:left}}
.dlg .ok{{display:inline-block;padding:8px 28px;border-radius:18px;border:0;background:#111;color:#fff;font-size:13px;cursor:pointer}}
.dlg .ok:active{{opacity:.85}}
</style>
</head><body>
<h3>{title}</h3>
<p class="meta">{source} · 发布于 {published}</p>
{cover_html}

<button class="btn primary" id="btnBrowser">📋 复制浏览器链接</button>
<button class="btn secondary" id="btnShare">📋 复制抖音口令</button>

<div class="section">
  <div class="label">🌐 浏览器链接（点击上方按钮一键复制；或长按下方文本手动选中复制）</div>
  <div class="value" id="vBrowser">{share_url_text}</div>
</div>
<div class="section">
  <div class="label">📨 抖音口令（点击上方按钮一键复制；或长按下方文本手动选中复制）</div>
  <div class="value" id="vShare">{share_text}</div>
</div>

<div class="mask" id="mask">
  <div class="dlg">
    <div class="icon" id="dlgIcon">✓</div>
    <div class="title" id="dlgTitle">已复制</div>
    <div class="msg" id="dlgMsg"></div>
    <button class="ok" onclick="document.getElementById('mask').classList.remove('show')">知道了</button>
  </div>
</div>

<script>
(function(){{
  var data = {data_json};

  function tryCopy(text){{
    if (navigator.clipboard && window.isSecureContext) {{
      return navigator.clipboard.writeText(text).then(function(){{ return true; }}).catch(function(){{ return fallback(text); }});
    }}
    return Promise.resolve(fallback(text));
  }}
  function fallback(text){{
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position='fixed'; ta.style.top='-1000px'; ta.style.left='-1000px';
    ta.style.opacity='0'; ta.setAttribute('readonly','');
    document.body.appendChild(ta);
    ta.focus(); ta.select();
    try {{ ta.setSelectionRange(0, text.length); }} catch(e) {{}}
    var ok = false;
    try {{ ok = document.execCommand('copy'); }} catch(e) {{}}
    document.body.removeChild(ta);
    return ok;
  }}
  function showDlg(title, msg){{
    document.getElementById('dlgTitle').textContent = title;
    document.getElementById('dlgMsg').innerHTML = msg;
    document.getElementById('mask').classList.add('show');
  }}
  // 兜底：若 execCommand 在某些 WebView 上无效，下方文本块永远可长按选中复制
  var FALLBACK_HINT = '<br><br><span style="color:#888;font-size:12px;">如未自动复制，请长按下方对应文本块选中后复制。</span>';
  function doCopyUrl(){{
    Promise.resolve(tryCopy(data.url)).then(function(){{
      showDlg('如何使用', '请打开任意浏览器（Safari / Chrome / 微信外部浏览器均可），把链接<b>粘贴到地址栏</b>后回车，即可在网页里直接播放该作品。' + FALLBACK_HINT);
    }});
  }}
  function doCopyText(){{
    Promise.resolve(tryCopy(data.text)).then(function(){{
      showDlg('如何使用', '回到<b>抖音 App</b>，<b>点击顶部搜索框</b>并粘贴该口令后<b>点击搜索</b>，App 会自动跳转到这条作品的播放页。' + FALLBACK_HINT);
    }});
  }}
  document.getElementById('btnBrowser').addEventListener('click', doCopyUrl);
  document.getElementById('btnShare').addEventListener('click', doCopyText);

  // 从 mpnews 的"复制浏览器链接 / 复制抖音口令"两个按钮跳过来时带 ?copy=url 或 ?copy=text，
  // 落地即自动执行一次对应复制并弹窗解释怎么用。
  var qs = (location.search || '').toLowerCase();
  var auto = '';
  if (qs.indexOf('copy=url') >= 0) auto = 'url';
  else if (qs.indexOf('copy=text') >= 0 || qs.indexOf('copy=share') >= 0) auto = 'text';
  if (auto) {{
    setTimeout(function(){{ if (auto === 'url') doCopyUrl(); else doCopyText(); }}, 80);
  }}
}})();
</script>
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
    """生成/覆盖一个抖音作品的中转页，返回文件路径。"""
    cover_html = (
        f'<img class="cover" src="{html.escape(image_proxy_url)}" alt="cover">'
        if image_proxy_url else ""
    )
    safe_share_text = share_text or "（该条作品无口令文本，请直接在抖音 App 内搜索作者）"
    data_blob = json.dumps({"url": share_url or "", "text": safe_share_text}, ensure_ascii=False)
    page = _PAGE_TPL.format(
        title=html.escape(title or "抖音作品"),
        source=html.escape(source or ""),
        published=html.escape(published or ""),
        cover_html=cover_html,
        share_text=html.escape(safe_share_text),
        share_url_text=html.escape(share_url or ""),
        data_json=data_blob,
    )
    out = DY_PAGES_DIR / f"{aweme_id}.html"
    out.write_text(page, encoding="utf-8")
    return out


def page_file(aweme_id: str) -> Path:
    return DY_PAGES_DIR / f"{aweme_id}.html"
