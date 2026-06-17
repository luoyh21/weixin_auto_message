"""给航天新闻打主题标签。

基于译文（中文标题 + 正文）做关键词命中打分，选出 1 个主题标签；再附加一个
范围标签（国际新闻 / 国内航天）。约定：
- 标题只放 1 个标签 = tags[0]（主题，没有主题则用范围标签）
- 文章开头放全部标签（主题 + 范围）
"""
from __future__ import annotations

# 主题标签 → 关键词（按优先级从上到下；命中数相同取靠前者）
_TOPIC_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("载人航天", (
        "宇航员", "航天员", "载人", "空间站", "阿尔忒弥斯", "登月", "乘组", "出舱",
        "太空行走", "神舟", "天宫", "舱外", "驻留", "artemis",
    )),
    ("火箭发射", (
        "火箭", "发射", "首飞", "复飞", "运载", "星舰", "猎鹰", "长征", "阿丽亚娜",
        "ariane", "助推器", "入轨", "点火", "发射场", "一级", "二级", "回收",
    )),
    ("卫星应用", (
        "卫星", "遥感", "通信", "星座", "组网", "导航", "对地观测", "星链",
        "数据中心", "宽带", "批产", "生产线", "测控", "载荷",
    )),
    ("深空探测", (
        "探测器", "火星", "木星", "土星", "金星", "小行星", "彗星", "探月", "深空",
        "着陆", "轨道器", "采样", "巡视", "登陆器",
    )),
    ("天文科学", (
        "望远镜", "黑洞", "星系", "超新星", "天文", "宇宙", "韦伯", "钱德拉", "哈勃",
        "银河", "恒星", "系外行星", "遗迹", "暗物质", "引力波", "考古",
    )),
    ("政策军事", (
        "太空军", "国防", "预算", "法案", "参议院", "政策", "军事", "金穹", "导弹",
        "整合", "监管", "拨款", "授权", "战略", "司令部",
    )),
    ("商业航天", (
        "商业", "公司", "融资", "签署", "意向书", "合同", "初创", "创业", "市场",
        "航天港", "私营", "中标", "投资",
    )),
]


def classify_topic(text: str) -> str:
    """返回最匹配的主题标签；无命中返回空串。"""
    low = (text or "").lower()
    best, best_score = "", 0
    for topic, kws in _TOPIC_KEYWORDS:
        score = sum(1 for kw in kws if kw in low)
        if score > best_score:
            best, best_score = topic, score
    return best


def tags_for(text: str, scope: str = "国际新闻") -> list[str]:
    """返回标签列表：[主题, 范围]（去重、保序）；无主题则只返回 [范围]。"""
    out: list[str] = []
    topic = classify_topic(text)
    if topic:
        out.append(topic)
    if scope and scope not in out:
        out.append(scope)
    return out or [scope]


def tag_prefix(tags: list[str]) -> str:
    """标题用：只取第一个标签，形如 `#国际新闻 `（含尾随空格）。"""
    if not tags:
        return ""
    return f"#{tags[0]} "


def tag_line(tags: list[str]) -> str:
    """正文开头用：全部标签，形如 `#卫星应用 #国际新闻`。"""
    return " ".join(f"#{t}" for t in tags)
