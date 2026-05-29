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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", required=True, help="缓存文件名（不含扩展名），例如 evening_2026-05-27")
    p.add_argument("--to", default=None, help="覆盖收件人 UserId（管道分隔）")
    p.add_argument("--luoyihe", action="store_true", help="仅发给 LuoYiHe（测试用）")
    args = p.parse_args()

    if args.to:
        os.environ["WECOM_TO_USER"] = args.to
    elif args.luoyihe:
        os.environ["WECOM_TO_USER"] = "LuoYiHe"

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

    # 必须在 env 覆盖后再 import，否则 SETTINGS 已固化旧值
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

    cache_path = SETTINGS.cache_dir / f"{args.cache}.json"
    if not cache_path.exists():
        logging.error("cache file not found: %s", cache_path)
        return 2
    rec = json.loads(cache_path.read_text("utf-8"))

    summary = rec.get("summary", "")
    sn = rec.get("spacenews", []) or []
    douyin = rec.get("douyin", []) or []

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
        os.environ.get("WECOM_TO_USER", "(env default)"),
        len(head.encode("utf-8")), len(news_cards),
        sum(1 for c in news_cards if not c["title"].startswith("[抖音")),
        sum(1 for c in news_cards if c["title"].startswith("[抖音")),
        hero_image_url[:80],
    )

    results = []
    if head:
        results.extend(send_text(head))
    if news_cards:
        results.append(send_news(news_cards))

    ok = all(r and r.get("errcode") == 0 for r in results)
    print(f"\nresend done: sent={ok}, parts={len(results)}")
    for r in results:
        print(" ", r)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
