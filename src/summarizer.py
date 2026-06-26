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


SYS_PAPER = (
    "你是航天领域的资料编辑。用户会给你一篇学术论文 / 技术报告的标题、要点提示，"
    "以及一段可能含目录、页码、作者信息或排版噪声的原文片段。\n"
    "请用简体中文写一段**连贯的介绍**，说明这篇论文/报告做了什么、研究或盘点了哪些内容、"
    "得出什么结论或有什么意义。要求：\n"
    "1. 3~6 句话，自然成段，面向科普读者；\n"
    "2. **绝不**罗列目录、章节号、页码，**不要**逐句翻译原文，不要出现参考文献、邮箱、版权声明等噪声；\n"
    "3. 聚焦『做了什么、有什么价值』，可适当结合要点提示；\n"
    "4. 不要使用『本文』之外的第一人称，不要加标题或编号。"
)


def summarize_paper(title: str, raw_text: str = "", hint: str = "") -> str:
    """把学术论文/报告（常为 PDF，正文是目录或排版噪声）总结成一段中文简介。"""
    user = (
        f"标题：{title}\n\n"
        f"要点提示：{hint or '（无）'}\n\n"
        f"原文片段（可能含目录/噪声，仅供参考）：\n{(raw_text or '')[:4000]}"
    )
    resp = client().chat.completions.create(
        model=SETTINGS.openai_model,
        messages=[
            {"role": "system", "content": SYS_PAPER},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()


SYS_TRANSLATE = (
    "你是航天领域的中文编辑。用户会给你一条英文航天资讯（标题 + 正文）。\n"
    "请翻译并精炼成简体中文，只输出一个 JSON 对象，不要任何额外文字、不要 markdown：\n"
    "{\n"
    '  "title": "不超过30字的简体中文标题（必填，忠实原意，不照抄英文）",\n'
    '  "summary": "用简体中文把正文概括成2~4句（约120字以内），保留关键数字/机构/技术名词；正文为空则据标题给一句话简介"\n'
    "}\n"
    "术语用航天业界通行中文译名；个别专有缩写可中英并存。不要寒暄、不要逐句直译堆砌。"
)


def translate_zh(title: str, text: str = "") -> dict:
    """把英文航天资讯翻成中文，返回 {"title": 中文标题, "summary": 中文摘要}。

    失败时回退英文原文（title 原样、summary 取正文/标题截断），保证调用方仍可入库。
    """
    import json as _json

    title = (title or "").strip()
    text = (text or "").strip()
    if not title and not text:
        return {"title": "", "summary": ""}
    user = f"标题：{title}\n\n正文：\n{text[:3000]}"
    try:
        resp = client().chat.completions.create(
            model=SETTINGS.openai_model,
            messages=[
                {"role": "system", "content": SYS_TRANSLATE},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        data = _json.loads(resp.choices[0].message.content.strip())
        zh_title = (data.get("title") or "").strip()
        zh_summary = (data.get("summary") or "").strip()
        if zh_title or zh_summary:
            return {"title": zh_title or title, "summary": zh_summary or text[:140]}
    except Exception as e:
        log.warning("translate_zh failed: %s", e)
    return {"title": title, "summary": (text or title)[:140]}


SYS_SOCIAL = (
    "你是航天领域的情报分析编辑。用户会给你一条政要（如马斯克、特朗普）在社交媒体（X / Truth Social）"
    "上的帖子原文（英文为主）。\n"
    "**无论内容是否涉及航天，都要给出中文标题和整段中文翻译**；是否给出『解读』则取决于内容是否与航天器/太空相关。\n"
    "【space_related 判定口径】仅当帖子**明确涉及**以下之一才为 true：\n"
    "  · 火箭/运载器、卫星、飞船、载人航天、空间站、星舰(Starship)、星链(Starlink)；\n"
    "  · 火箭发射 / 试飞 / 在轨任务 / 航天事故或里程碑；\n"
    "  · NASA / SpaceX / 蓝色起源 等航天机构与商业航天公司及其动态；\n"
    "  · 太空军(Space Force) 及太空军事 / 反卫星 / 太空态势感知 / 天基防御；\n"
    "  · 登月 / 火星 / 深空探测 / 行星科学 / 太空望远镜；\n"
    "  · 与**航天**直接相关的政策 / 预算 / 立法 / 拨款 / 监管（FAA 航天发射许可、FCC 卫星等）。\n"
    "  普通航空、无人机、常规军事、国内政治、关税、体育等与太空无关的内容，space_related=false。\n"
    "只输出一个 JSON 对象，不要任何额外文字、不要 markdown 代码块，字段如下：\n"
    '{\n'
    '  "space_related": true/false,\n'
    '  "title": "不超过20字的中文标题（必填）",\n'
    '  "translation": "把帖子正文**整段翻译成简体中文**（务必是中文，不能照抄英文原文；必填）；翻译时仅将其中的链接(URL)、@用户名、#话题标签按原样保留、不翻译、不删除，其余文字一律译成中文",\n'
    '  "analysis": "仅当 space_related=true 且确有可点评之处（实质航天信息、值得点评其信号/影响）时，给出不超过200字的航天视角解读；其余情况（与航天无关、或只是转述/无可深入）一律留空字符串 \\"\\""\n'
    "}\n"
    "解读要客观专业、不复述原文、不寒暄；与航天无关或无可深入之处一律留空。"
)


def analyze_social_post(author_name: str, text: str, platform: str = "") -> dict:
    """对一条政要社媒帖子做：中文标题 + 整段中文翻译 + （仅航天相关时）航天视角解读。

    返回 {space_related: bool, title: str, translation: str, analysis: str}。
    帖子一律入库展示，不再按相关性丢弃；异常时回退为空字段（仍可入库，展示原文）。
    """
    import json as _json

    user = (
        f"作者：{author_name}\n"
        f"平台：{platform or '社交媒体'}\n"
        f"帖子原文：\n{(text or '').strip()[:2000]}"
    )
    try:
        resp = client().chat.completions.create(
            model=SETTINGS.openai_model,
            messages=[
                {"role": "system", "content": SYS_SOCIAL},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        data = _json.loads(raw)
    except Exception as e:
        log.warning("analyze_social_post failed: %s", e)
        return {"space_related": False, "title": "", "translation": "", "analysis": ""}

    return {
        "space_related": bool(data.get("space_related")),
        "title": (data.get("title") or "").strip(),
        "translation": (data.get("translation") or "").strip(),
        "analysis": (data.get("analysis") or "").strip(),
    }


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
