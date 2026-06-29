"""海外抓取端的图片下载助手（在 GitHub Actions / 海外节点跑）。

国内服务器访问不到推特/Truth、且被 nasaspaceflight 等盗链拦截，所以由海外这侧
把图片字节下载好、base64 后随帖子/文章回传，国内服务器落盘并本地直供。

仅依赖 requests + 标准库，可被两个 scrape 脚本直接 `import img_relay` 复用。
"""
from __future__ import annotations

import base64
import re
import sys
import time
from urllib.parse import quote, unquote

import requests

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MAX_BYTES = 8 * 1024 * 1024

# 这些 host 国内手机/服务器直连通常没问题，无需回传以省流量
DIRECT_OK_HOSTS = (
    "i0.wp.com", "i1.wp.com", "i2.wp.com",
    "nasa.gov", "spaceflightnow.com", "spacenews.com",
)


def _host(url: str) -> str:
    try:
        return url.split("://", 1)[-1].split("/", 1)[0].lower()
    except Exception:
        return ""


def need_relay(url: str) -> bool:
    """国际要闻图：仅对国内拿不到/盗链的 host 回传，直连可用的跳过。"""
    if not url:
        return False
    h = _host(url)
    return not any(h == d or h.endswith("." + d) or h.endswith(d) for d in DIRECT_OK_HOSTS)


def weserv(url: str) -> str:
    """把任意源图 URL 包成 images.weserv.nl 公共图片代理 URL。

    weserv 在其服务端发起请求，能绕开 pbs.twimg.com / truthsocial CDN 对机房 IP
    的封禁与盗链拦截，作为直连失败后的兜底下载源。
    """
    if not url:
        return ""
    stripped = url.split("://", 1)[-1]
    return f"https://images.weserv.nl/?url={quote(stripped, safe='')}"


def nitter_to_twimg(url: str) -> str:
    """把 nitter 的 /pic/ 代理地址还原成真实 pbs.twimg.com 地址。

    例: https://nitter.net/pic/media%2FHLcJUtcbgAA6ZCl.jpg
        -> https://pbs.twimg.com/media/HLcJUtcbgAA6ZCl.jpg
    """
    if not url:
        return url
    m = re.search(r"/pic/(.+)$", url)
    if not m:
        return url
    path = unquote(m.group(1))
    path = path.lstrip("/")
    return "https://pbs.twimg.com/" + path


def _default_referer(url: str) -> str:
    """没有显式 referer 时，按图源 host 给一个合理的 Referer。
    truthsocial 的 static-assets CDN 对带本站 Referer 的请求更友好。"""
    h = _host(url)
    if "truthsocial" in h:
        return "https://truthsocial.com/"
    if "twimg" in h:
        return "https://twitter.com/"
    return ""


def download_as_b64(url: str, referer: str | None = None) -> tuple[str, str] | tuple[None, None]:
    """下载图片，返回 (base64字符串, mime)。失败返回 (None, None)。带 2 次重试。"""
    if not url or not url.startswith(("http://", "https://")):
        return None, None
    headers = {
        "User-Agent": _UA,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    ref = referer or _default_referer(url)
    if ref:
        headers["Referer"] = ref
    r = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=25)
            if r.status_code == 200 and r.content:
                break
            print(f"  [relay] {url} -> HTTP {r.status_code} (try {attempt + 1})", file=sys.stderr)
        except Exception as e:
            print(f"  [relay] {url} -> {e} (try {attempt + 1})", file=sys.stderr)
            r = None
        time.sleep(1.0 * (attempt + 1))
    if r is None or r.status_code != 200 or not r.content:
        return None, None
    data = r.content
    if len(data) > MAX_BYTES:
        print(f"  [relay] {url} -> too large ({len(data)})", file=sys.stderr)
        return None, None
    ct = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        # 有些 CDN 不给正确 content-type，按扩展名兜底
        low = url.lower().split("?")[0]
        ct = ("image/png" if low.endswith(".png") else
              "image/webp" if low.endswith(".webp") else
              "image/gif" if low.endswith(".gif") else "image/jpeg")
    return base64.b64encode(data).decode("ascii"), ct


def download_best(urls: list[str], referer: str | None = None) -> tuple[str, str] | tuple[None, None]:
    """依次尝试多个候选图片 URL，命中即返回 (base64, mime)；每个候选直连失败后
    再用 images.weserv.nl 公共代理兜底一次。全部失败返回 (None, None)。

    用于政要社媒图片：X 图优先用 RSS 来源的 nitter /pic 直连（与 RSS 同实例，最稳），
    再退化到 pbs.twimg.com，最后 weserv 兜底；Truth Social 直连失败也走 weserv。
    """
    tried: set[str] = set()
    for u in urls:
        if not u or u in tried:
            continue
        tried.add(u)
        b64, mime = download_as_b64(u, referer=referer)
        if b64:
            return b64, mime
        ws = weserv(u)
        if ws and ws not in tried:
            tried.add(ws)
            b64, mime = download_as_b64(ws)
            if b64:
                return b64, mime
    return None, None
