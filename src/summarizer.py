"""每日总览 / 问答：默认走 TextRank（不调用 LLM），可通过 env 切回 GPT。

环境变量：
- `SUMMARIZER_USE_LLM=1`：daily_summary 走原 GPT 路径（要求 OPENAI_API_KEY 可用）。
- 缺省（=0）：完全本地化，基于 textrank4zh 做抽取式总览 + 标题清单。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterable

from openai import OpenAI

from .config import SETTINGS
from .translate import translate_to_zh, _apply_glossary

log = logging.getLogger(__name__)

_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=SETTINGS.openai_api_key, base_url=SETTINGS.openai_base_url)
    return _client


def _articles_block(articles: Iterable[dict]) -> str:
    lines: list[str] = []
    for i, a in enumerate(articles, 1):
        lines.append(
            f"[{i}] 来源: {a.get('source','')}\n"
            f"标题: {a.get('title','')}\n"
            f"链接: {a.get('link','')}\n"
            f"发布: {a.get('published','')}\n"
            f"摘要: {(a.get('summary') or a.get('description') or '').strip()}\n"
        )
    return "\n".join(lines)


_COMMON_RULES = (
    "你是一名航天新闻日报编辑。基于用户提供的原始材料，用中文撰写『航天速递』。要求：\n"
    "1. 顶部用 1~2 句中文写一段总览（50~80 字），点出今日 2~3 个核心看点，言简意赅；"
    "总览不要带编号或标题；\n"
    "2. 每条按【编号 + markdown 链接】格式独占一行，链接文字就是一句精炼的中文概括"
    "（25~55 字），URL 只能写在括号里，例如：\n"
    "       1. [SpaceX 因地面设备故障取消星舰 V3 首飞](原文URL)\n"
    "   不要再带英文标题、不要『——』或冒号、不要把裸 URL 写到正文里。\n"
    "3. 严禁编造材料里不存在的新闻、严禁把同一条新闻在不同板块里重复出现；\n"
    "4. 不要输出任何分隔线（如『---』『===』），不要重复『航天速递』标题，"
    "总字数控制在 1200 中文字符以内。\n"
    "**专有名词须按以下译法**（即便原始材料里写的是别的中文也要改正过来）："
    "Golden Dome / Golden Dome for America → 金穹计划（不要写成『金顶』『金顶计划』『金圆顶』）；"
    "Iron Dome → 铁穹；Space Force → 美国太空军；Artemis → 阿尔忒弥斯；"
    "Starship → 星舰；Falcon 9 → 猎鹰 9；Starlink → 星链。\n"
)

SYS_DAILY_BOTH = _COMMON_RULES + (
    "5. 必须分两个板块输出：『🌍 国际要闻』（最多 8 条）和『📰 公众号精选』（最多 5 条），"
    "每个板块各自从 1 开始独立编号；两个板块的链接必须不重复。"
)

SYS_DAILY_INTL_ONLY = _COMMON_RULES + (
    "5. 本次没有公众号材料，只输出『🌍 国际要闻』一个板块，最多 8 条；"
    "禁止输出『📰 公众号精选』板块或任何与公众号有关的字样。"
)

SYS_DAILY_GZH_ONLY = _COMMON_RULES + (
    "5. 本次没有国际要闻材料，只输出『📰 公众号精选』一个板块，最多 5 条；"
    "禁止输出『🌍 国际要闻』板块或任何与国际要闻有关的字样。"
)


_SESSION_OPENING_PREFIX = {
    "早间": "早安航天，今日关注：",
    "晚间": "今日航天盘点：",
}


def _session_opening_hint(session_label: str) -> str:
    prefix = _SESSION_OPENING_PREFIX.get(session_label)
    if not prefix:
        return ""
    return (
        f"总览的第一行必须**严格、原样**以『{prefix}』开头（包含中文标点），"
        "其后紧接着一句话点出今日 2~3 个核心看点。"
        "不要把『{prefix}』改写、翻译或加任何前缀，也不要在它之前添加任何字符。"
    )


def daily_summary(
    spacenews: list[dict],
    opml_entries: list[dict],
    session_label: str = "每日",
) -> str:
    if not spacenews and not opml_entries:
        return "今日未抓取到任何新文章。"
    if os.getenv("SUMMARIZER_USE_LLM", "0").strip() == "1":
        return _daily_summary_llm(spacenews, opml_entries, session_label)
    return _daily_summary_textrank(spacenews, opml_entries, session_label)


def _daily_summary_llm(
    spacenews: list[dict],
    opml_entries: list[dict],
    session_label: str,
) -> str:
    has_intl = bool(spacenews)
    has_gzh = bool(opml_entries)
    if has_intl and has_gzh:
        system_prompt = SYS_DAILY_BOTH
    elif has_intl:
        system_prompt = SYS_DAILY_INTL_ONLY
    else:
        system_prompt = SYS_DAILY_GZH_ONLY

    opening_hint = _session_opening_hint(session_label)
    if opening_hint:
        system_prompt = system_prompt + "\n6. " + opening_hint

    parts: list[str] = []
    if has_intl:
        parts.append("## 国际要闻（spacelive 聚合）\n" + _articles_block(spacenews))
    if has_gzh:
        parts.append("## 公众号 (OPML)\n" + _articles_block(opml_entries))
    user_msg = "以下为今天的原始抓取材料：\n\n" + "\n\n".join(parts)

    resp = client().chat.completions.create(
        model=SETTINGS.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.4,
    )
    text = resp.choices[0].message.content.strip()

    prefix = _SESSION_OPENING_PREFIX.get(session_label)
    if prefix:
        lines = text.split("\n", 1)
        first = lines[0].lstrip()
        if not first.startswith(prefix):
            stripped = re.sub(r"^[\s『「【\"\'：:,，。.]*", "", first)
            stripped = re.sub(
                r"^(今[日晨晚天天]?[航天]*[领]?[领域]?[聚关注盘点回顾速览要事览]+[：:，,]?)\s*",
                "", stripped,
            )
            lines[0] = prefix + stripped
            text = "\n".join(lines)
    return text


# ---------- TextRank 抽取式总览 ----------


def _zh_text_for(a: dict) -> str:
    """返回一条材料用于 TextRank 的中文文本：优先用译文正文，回退到摘要。"""
    body = (a.get("body_zh") or "").strip()
    if body:
        return body
    summary = (a.get("summary") or a.get("description") or "").strip()
    if summary:
        # OPML 摘要本来就是中文；SpaceNews 的 summary 是英文，需现场翻一下用于抽取
        if re.search(r"[\u4e00-\u9fff]", summary):
            return summary
        try:
            return translate_to_zh(summary)
        except Exception as e:
            log.debug("textrank: summary translate failed: %s", e)
            return ""
    return ""


def _zh_title_for(a: dict) -> str:
    t = (a.get("title_zh") or "").strip()
    if t:
        return t
    t = (a.get("title") or "").strip()
    if not t:
        return ""
    if re.search(r"[\u4e00-\u9fff]", t):
        return t
    try:
        return translate_to_zh(t).strip()
    except Exception:
        return t


def _textrank_top_sentences(text: str, num: int = 2) -> list[str]:
    """从中文长文本里抽 num 句最 representative 的句子。"""
    text = (text or "").strip()
    if not text:
        return []
    try:
        from textrank4zh import TextRank4Sentence
    except Exception as e:
        log.warning("textrank4zh not available: %s", e)
        # 回退：取前 num 句
        sents = re.split(r"(?<=[。！？!?])", text)
        return [s.strip() for s in sents if s.strip()][:num]
    try:
        tr = TextRank4Sentence()
        tr.analyze(text=text, lower=True, source="all_filters")
        items = tr.get_key_sentences(num=num)
        return [it.sentence.strip() for it in items if it.sentence.strip()]
    except Exception as e:
        log.warning("textrank analyze failed: %s", e)
        sents = re.split(r"(?<=[。！？!?])", text)
        return [s.strip() for s in sents if s.strip()][:num]


def _truncate_zh(s: str, max_chars: int) -> str:
    s = s.strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip("，。、；：") + "…"


def _one_sentence_for(article: dict, max_chars: int = 55) -> str:
    """从一条材料里抽一句话总结。

    - 优先在译文正文上跑 TextRank 取 top-1 句；
    - 没有正文（抓取失败）就退回 OPML/spacelive 自带的中文摘要的首句；
    - 再没有就用标题兜底。
    """
    text = _zh_text_for(article)
    if text:
        sents = _textrank_top_sentences(text, num=1)
        if sents:
            return _truncate_zh(_clean_sentence(sents[0]), max_chars)
        # TextRank 无结果就退到首句
        first = re.split(r"(?<=[。！？!?])", text, maxsplit=1)[0]
        if first.strip():
            return _truncate_zh(_clean_sentence(first), max_chars)
    title = _zh_title_for(article)
    return _truncate_zh(_clean_sentence(title), max_chars) if title else ""


def _clean_sentence(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^[\s—\-—\u2013\u2014『「【\"\'：:,，。.]+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.rstrip("。.；;，,")


def _daily_summary_textrank(
    spacenews: list[dict],
    opml_entries: list[dict],
    session_label: str,
) -> str:
    sn_titles = [_zh_title_for(a) for a in spacenews]
    gzh_titles = [_zh_title_for(e) for e in opml_entries]
    sn_links = [a.get("link", "") for a in spacenews]
    gzh_links = [e.get("link", "") for e in opml_entries]

    # 总览：每条单独抽一句话总结，再用「；」拼起来。
    # 控制条数（最多 3 条国际 + 2 条公众号），整体不超过 220 字符。
    overview_pieces: list[str] = []
    for a in spacenews[:3]:
        s = _one_sentence_for(a, max_chars=55)
        if s:
            overview_pieces.append(s)
    for e in opml_entries[:2]:
        s = _one_sentence_for(e, max_chars=55)
        if s:
            overview_pieces.append(s)

    if overview_pieces:
        overview = _apply_glossary("；".join(overview_pieces))
        overview = _truncate_zh(overview, 220) + "。"
    else:
        seeds = [t for t in (sn_titles + gzh_titles) if t][:2]
        overview = ("、".join(seeds) + "等今日要闻。") if seeds else "今日航天速递。"
        overview = _apply_glossary(overview)

    prefix = _SESSION_OPENING_PREFIX.get(session_label, "")
    if prefix and not overview.startswith(prefix):
        overview = prefix + overview

    lines: list[str] = [overview, ""]
    if spacenews:
        lines.append("🌍 国际要闻")
        for i, (t, url) in enumerate(zip(sn_titles, sn_links), 1):
            if i > 8:
                break
            t = t or url
            lines.append(f"{i}. [{t}]({url})")
        lines.append("")
    if opml_entries:
        lines.append("📰 公众号精选")
        for i, (t, url) in enumerate(zip(gzh_titles, gzh_links), 1):
            if i > 5:
                break
            t = t or url
            lines.append(f"{i}. [{t}]({url})")
    return "\n".join(lines).strip()


SYS_QA = (
    "你是一个航天主题的助手。你会得到『最近一次抓取的新闻原始材料』作为背景知识。\n"
    "请用简体中文、简洁友好地回答用户的提问。\n"
    "**硬性要求**：每当回答中提到背景材料里的一条具体新闻时，必须用 markdown 链接的形式"
    "把该新闻的中文标题包起来，URL 取材料中对应条目的『链接』字段，例如：\n"
    "    [神舟二十三号顺利对接](https://links.he-ting.com/news/...)\n"
    "禁止只写裸 URL，禁止只写标题不带链接。如果回答涉及多条新闻，逐条都加链接。\n"
    "如果问题与材料无关，可结合常识回答，无需强行附链接，但应保持精炼。"
)


def answer_with_context(question: str, context_articles: list[dict]) -> str:
    bg = _articles_block(context_articles) if context_articles else "（暂无昨日材料）"
    resp = client().chat.completions.create(
        model=SETTINGS.openai_model,
        messages=[
            {"role": "system", "content": SYS_QA},
            {"role": "user", "content": f"【昨日新闻材料】\n{bg}\n\n【用户提问】\n{question}"},
        ],
        temperature=0.5,
    )
    return resp.choices[0].message.content.strip()
