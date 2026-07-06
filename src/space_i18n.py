"""航天术语中文化：发射提供方 / 发射场（地点）等英文名的中文映射。

用于「每日发射」「未来发射」把 The Space Devs 返回的英文 provider / location
描述为中文。策略：先精确/别名匹配，再关键词包含匹配，命中不了则原样返回英文
（保证不丢信息）。全部为静态映射、零 LLM 调用，稳定可复现。
"""
from __future__ import annotations

# 发射服务提供方：LL2 launch_service_provider.name -> 中文（保留英文缩写便于识别）
_PROVIDER: dict[str, str] = {
    "SpaceX": "太空探索技术公司(SpaceX)",
    "National Aeronautics and Space Administration": "美国国家航空航天局(NASA)",
    "United Launch Alliance": "联合发射联盟(ULA)",
    "Rocket Lab": "火箭实验室(Rocket Lab)",
    "Rocket Lab USA": "火箭实验室(Rocket Lab)",
    "Blue Origin": "蓝色起源(Blue Origin)",
    "Arianespace": "阿丽亚娜航天公司(Arianespace)",
    "Northrop Grumman": "诺斯罗普·格鲁曼(Northrop Grumman)",
    "Northrop Grumman Space Systems": "诺斯罗普·格鲁曼空间系统",
    "Firefly Aerospace": "萤火虫航天(Firefly)",
    "Relativity Space": "相对论空间(Relativity)",
    "Virgin Galactic": "维珍银河(Virgin Galactic)",
    "Virgin Orbit": "维珍轨道(Virgin Orbit)",
    "Astra": "阿斯特拉(Astra)",
    "ABL Space Systems": "ABL 航天系统",
    "Roscosmos": "俄罗斯航天国家集团(Roscosmos)",
    "Russian Federal Space Agency (ROSCOSMOS)": "俄罗斯航天国家集团(Roscosmos)",
    "Indian Space Research Organization": "印度空间研究组织(ISRO)",
    "Japan Aerospace Exploration Agency": "日本宇宙航空研究开发机构(JAXA)",
    "European Space Agency": "欧洲空间局(ESA)",
    "Iranian Space Agency": "伊朗航天局",
    # 中国
    "China Aerospace Science and Technology Corporation": "中国航天科技集团(CASC)",
    "China Aerospace Science and Industry Corporation": "中国航天科工集团(CASIC)",
    "Expace": "航天科工火箭技术公司(Expace)",
    "ExPace": "航天科工火箭技术公司(Expace)",
    "Galactic Energy": "星河动力(Galactic Energy)",
    "LandSpace": "蓝箭航天(LandSpace)",
    "iSpace": "星际荣耀(iSpace)",
    "i-Space": "星际荣耀(iSpace)",
    "Space Pioneer": "天兵科技(Space Pioneer)",
    "Orienspace": "东方空间(Orienspace)",
    "CAS Space": "中科宇航(CAS Space)",
    "Deep Blue Aerospace": "深蓝航天",
}

# 提供方关键词兜底（精确匹配不中时用包含匹配）
_PROVIDER_KW: list[tuple[str, str]] = [
    ("SpaceX", "太空探索技术公司(SpaceX)"),
    ("United Launch Alliance", "联合发射联盟(ULA)"),
    ("Rocket Lab", "火箭实验室(Rocket Lab)"),
    ("Blue Origin", "蓝色起源(Blue Origin)"),
    ("Arianespace", "阿丽亚娜航天公司(Arianespace)"),
    ("Northrop Grumman", "诺斯罗普·格鲁曼(Northrop Grumman)"),
    ("Firefly", "萤火虫航天(Firefly)"),
    ("Roscosmos", "俄罗斯航天国家集团(Roscosmos)"),
    ("Science and Technology Corporation", "中国航天科技集团(CASC)"),
    ("Science and Industry", "中国航天科工集团(CASIC)"),
    ("Galactic Energy", "星河动力(Galactic Energy)"),
    ("LandSpace", "蓝箭航天(LandSpace)"),
    ("ISRO", "印度空间研究组织(ISRO)"),
    ("JAXA", "日本宇宙航空研究开发机构(JAXA)"),
    ("NASA", "美国国家航空航天局(NASA)"),
]

# 发射场 / 地点：用关键词包含匹配（LL2 的地点串常带州/国后缀，精确匹配不稳）
_PLACE_KW: list[tuple[str, str]] = [
    ("Cape Canaveral", "卡纳维拉尔角太空军基地（美国佛州）"),
    ("Kennedy Space Center", "肯尼迪航天中心（美国佛州）"),
    ("Vandenberg", "范登堡太空军基地（美国加州）"),
    ("Wallops", "沃洛普斯飞行基地（美国弗州）"),
    ("Kwajalein", "夸贾林环礁（马绍尔群岛）"),
    ("Boca Chica", "博卡奇卡星际基地（美国得州）"),
    ("Starbase", "星际基地（美国得州）"),
    ("Jiuquan", "酒泉卫星发射中心"),
    ("Xichang", "西昌卫星发射中心"),
    ("Taiyuan", "太原卫星发射中心"),
    ("Wenchang", "文昌航天发射场"),
    ("Dongfeng", "东风商业航天创新试验区（酒泉）"),
    ("Haiyang", "海阳（山东）海上发射母港"),
    ("Yellow Sea", "黄海海域（海上发射）"),
    ("Baikonur", "拜科努尔航天发射场（哈萨克斯坦）"),
    ("Plesetsk", "普列谢茨克航天发射场（俄罗斯）"),
    ("Vostochny", "东方航天发射场（俄罗斯）"),
    ("Satish Dhawan", "萨迪什·达万航天中心（印度）"),
    ("Sriharikota", "斯里赫里戈达（印度）"),
    ("Guiana", "圭亚那航天中心（法属圭亚那）"),
    ("Kourou", "库鲁（法属圭亚那）"),
    ("Tanegashima", "种子岛宇宙中心（日本）"),
    ("Uchinoura", "内之浦宇宙空间观测所（日本）"),
    ("Mahia", "玛希亚半岛（新西兰）"),
    ("Rocket Lab Launch Complex 1", "火箭实验室1号发射场（新西兰）"),
    ("New Zealand", "新西兰"),
    ("Semnan", "塞姆南航天中心（伊朗）"),
    ("Air launch to orbit", "空中发射入轨"),
    ("Naro", "罗老宇航中心（韩国）"),
]

# 国家/后缀名规范化（出现在地点串尾部时替换成中文，提升可读性）
_COUNTRY_KW: list[tuple[str, str]] = [
    ("People's Republic of China", "中国"),
    ("United States of America", "美国"),
    ("USA", "美国"),
    ("Russian Federation", "俄罗斯"),
    ("Republic of Kazakhstan", "哈萨克斯坦"),
    ("Kazakhstan", "哈萨克斯坦"),
    ("French Guiana", "法属圭亚那"),
    ("Japan", "日本"),
    ("India", "印度"),
    ("New Zealand", "新西兰"),
    ("Iran", "伊朗"),
    ("South Korea", "韩国"),
]


def provider_zh(name: str) -> str:
    """发射提供方英文名 -> 中文（含缩写）。命中不了返回原文。"""
    s = (name or "").strip()
    if not s:
        return ""
    if s in _PROVIDER:
        return _PROVIDER[s]
    for kw, zh in _PROVIDER_KW:
        if kw.lower() in s.lower():
            return zh
    return s


def place_zh(name: str) -> str:
    """发射场/地点英文串 -> 中文。命中不了时仅把尾部国家名中文化，其余保留。"""
    s = (name or "").strip()
    if not s:
        return ""
    for kw, zh in _PLACE_KW:
        if kw.lower() in s.lower():
            return zh
    out = s
    for kw, zh in _COUNTRY_KW:
        if kw in out:
            out = out.replace(kw, zh)
    return out
