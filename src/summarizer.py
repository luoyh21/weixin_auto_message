"""调用 OpenAI 兼容接口做摘要 / 问答。"""
from __future__ import annotations

import logging
from typing import Iterable

from openai import OpenAI

from .config import SETTINGS

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
    has_intl = bool(spacenews)
    has_gzh = bool(opml_entries)
    if not has_intl and not has_gzh:
        return "今日未抓取到任何新文章。"

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

    # 安全网：强制开头匹配固定前缀。
    prefix = _SESSION_OPENING_PREFIX.get(session_label)
    if prefix:
        lines = text.split("\n", 1)
        first = lines[0].lstrip()
        if not first.startswith(prefix):
            # 去掉 GPT 自己写的开头短语，再拼上规定前缀
            import re as _re
            stripped = _re.sub(r"^[\s『「【\"\'：:,，。.]*", "", first)
            stripped = _re.sub(
                r"^(今[日晨晚天天]?[航天]*[领]?[领域]?[聚关注盘点回顾速览要事览]+[：:，,]?)\s*",
                "", stripped,
            )
            lines[0] = prefix + stripped
            text = "\n".join(lines)
    return text


SYS_QA = (
    "你是一个航天主题的助手。你会得到『最近一次抓取的新闻原始材料』作为背景知识。\n"
    "请用简体中文、简洁友好地回答用户的提问。\n"
    "**硬性要求**：每当回答中提到背景材料里的一条具体新闻时，必须用 markdown 链接的形式"
    "把该新闻的中文标题包起来，URL 取材料中对应条目的『链接』字段，例如：\n"
    "    [神舟二十三号顺利对接](http://8.130.209.181:8503/news/...)\n"
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
