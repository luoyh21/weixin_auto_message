"""把已有 cache 原样重发到 @all（或指定收件人）。

复用 daily.py 的卡片组装逻辑（每张卡片都带封面图 + 抖音中转页 + /img 代理），
但不做任何抓取 / 翻译 / 翻译页轮转。所有内容直接从 cache 文件读出。

用法：
    .venv/bin/python -m scripts.resend_cache --cache evening_2026-05-27          # 默认 @all
    .venv/bin/python -m scripts.resend_cache --cache evening_2026-05-27 --luoyihe
    .venv/bin/python -m scripts.resend_cache --cache morning_2026-05-27 --to LuoYiHe|WenYueJie
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def resend(cache_name: str, to_user: str | None = None) -> tuple[bool, list]:
    """从缓存重发一条半天速递给指定收件人（不重跑抓取/总结）。

    cache_name: 不含扩展名，例如 evening_2026-06-12
    to_user:    收件人 UserId（管道分隔）；None 则用 .env 默认（一般 @all）
    返回 (ok, results)
    """
    from src.config import SETTINGS
    from src.wecom import send_text, send_news
    from src.img_proxy import proxify as proxy_img, prefetch as prefetch_img
    from src.dy_pages import render_landing as render_dy_landing
    from src.daily import (
        _split_overview_and_list,
        _upgrade_image_to_full,
        _public_base,
        _pick_hero,
    )

    cache_path = SETTINGS.cache_dir / f"{cache_name}.json"
    if not cache_path.exists():
        logging.error("cache file not found: %s", cache_path)
        return False, []
    rec = json.loads(cache_path.read_text("utf-8"))

    summary = rec.get("summary", "")
    sn = rec.get("spacenews", []) or []
    douyin = rec.get("douyin", []) or []

    # 翻译页可能已被轮转删除 → 用缓存里的 body_zh 直接重建到磁盘，避免点开 404
    try:
        from src.news_pages import rebuild_pages_from_cache
        rebuild_pages_from_cache(sn)
    except Exception as e:
        logging.warning("rebuild news pages skipped: %s", e)

    head, tail = _split_overview_and_list(summary)

    # tail 已经是 HTML <a> 形式（cache 里存的是渲染后版本），用 <a> 正则抽出 link items
    a_pat = re.compile(r'<a\s+href="([^"]+)">([^<]+)</a>', re.I)
    link_items = [
        {"cn_title": m.group(2).strip(), "url": m.group(1).strip()}
        for m in a_pat.finditer(tail)
    ]

    # 复用 daily 里的 hero 选择
    hero_article, hero_candidates = _pick_hero(sn)
    hero_image_url = hero_candidates[0][0] if hero_candidates else rec.get("hero_image_url", "")

    if hero_article and link_items:
        hero_url = hero_article.get("link", "")
        link_items.sort(key=lambda x: 0 if x["url"] == hero_url else 1)

    # 反查每篇 SpaceNews 文章的封面 + Referer（按重写后的 /news/... 索引）
    sn_pic_map: dict[str, tuple[str, str]] = {}
    for a in sn:
        img = _upgrade_image_to_full(a.get("image_url") or "")
        if not img:
            continue
        ref = a.get("original_link") or a.get("link") or ""
        sn_pic_map[a.get("link", "")] = (img, ref)

    dy_max = int(os.getenv("DOUYIN_MAX_TOTAL", "0") or 0) or len(douyin)
    dy_reserve = min(len(douyin), max(0, dy_max))
    base_limit = max(0, 8 - dy_reserve)

    news_cards: list[dict] = []
    # ---- 国际/公众号新闻卡 ----
    for i, it in enumerate(link_items[:base_limit]):
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
            logging.info("drop picurl (prefetch failed): %s", src_img)
        news_cards.append(card)

    # ---- 抖音卡（cache 里的原始 douyin item 结构） ----
    for d in douyin[:dy_reserve]:
        aweme_id = d.get("aweme_id", "")
        image_url = d.get("image_url", "")
        ok_pic = bool(image_url) and prefetch_img(image_url, "https://www.iesdouyin.com/")
        pic_proxy_url = proxy_img(image_url, "https://www.iesdouyin.com/") if ok_pic else ""
        try:
            render_dy_landing(
                aweme_id,
                title=d.get("title", ""),
                source=d.get("source", ""),
                published=d.get("published", ""),
                share_text=d.get("share_text", ""),
                share_url=d.get("share_url") or d.get("link", ""),
                image_proxy_url=pic_proxy_url,
            )
            dy_url = f"{_public_base()}/dy/{aweme_id}"
        except Exception as e:
            logging.warning("render dy landing failed for %s: %s", aweme_id, e)
            dy_url = d.get("link", "")
        news_cards.append({
            "title": f"[抖音·{d.get('source','')}] {d.get('title','')}"[:120],
            "description": d.get("published", ""),
            "url": dy_url,
            "picurl": pic_proxy_url,
        })

    logging.info(
        "recipient=%s; head=%dB; news_cards=%d (sn=%d dy=%d); hero=%s",
        to_user or "(env default)",
        len(head.encode("utf-8")), len(news_cards),
        sum(1 for c in news_cards if not c["title"].startswith("[抖音")),
        sum(1 for c in news_cards if c["title"].startswith("[抖音")),
        hero_image_url[:80],
    )

    import time
    results = []
    if head:
        results.extend(send_text(head, to_user=to_user))
    # 订阅引导作为最后一栏并入图文消息，不在文字里放扫码链接、也不单独发送
    from src.daily import _promo_news_card
    news_cards.append(_promo_news_card())
    if news_cards:
        # 文字与图文之间留间隔，确保接收端按「先文字后图文」顺序展示
        if head:
            time.sleep(2)
        results.append(send_news(news_cards, to_user=to_user))

    ok = all(r and r.get("errcode") == 0 for r in results)
    return ok, results


def _cache_has_content(path: Path) -> bool:
    """缓存是否含真实文章（spacenews/opml/douyin 任一非空，且未被标记为无内容）。"""
    try:
        rec = json.loads(path.read_text("utf-8"))
    except Exception:
        return False
    if rec.get("skipped") == "no-new-content":
        return False
    return bool(rec.get("spacenews") or rec.get("opml") or rec.get("douyin"))


def latest_cache_name(require_content: bool = True) -> str | None:
    """返回最近一份半天缓存名（morning_/evening_）。

    require_content=True（默认）：跳过"今日无文章"的空缓存，返回最近一份**有内容**的，
    用于新成员补发"默认有效的第一条"。
    """
    from src.config import SETTINGS
    import glob
    files = sorted(
        glob.glob(str(SETTINGS.cache_dir / "morning_*.json"))
        + glob.glob(str(SETTINGS.cache_dir / "evening_*.json")),
        key=lambda p: Path(p).stat().st_mtime,
        reverse=True,
    )
    if not files:
        return None
    if require_content:
        for f in files:
            if _cache_has_content(Path(f)):
                return Path(f).stem
        return None  # 没有任何含内容的缓存
    return Path(files[0]).stem


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", required=True, help="缓存文件名（不含扩展名），例如 evening_2026-05-27")
    p.add_argument("--to", default=None, help="覆盖收件人 UserId（管道分隔）")
    p.add_argument("--luoyihe", action="store_true", help="仅发给 LuoYiHe（测试用）")
    args = p.parse_args()

    to_user = None
    if args.to:
        to_user = args.to
    elif args.luoyihe:
        to_user = "LuoYiHe"

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

    ok, results = resend(args.cache, to_user=to_user)
    print(f"\nresend done: sent={ok}, parts={len(results)}")
    for r in results:
        print(" ", r)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
