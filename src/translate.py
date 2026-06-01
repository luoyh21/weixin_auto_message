"""Non-LLM translation pipeline.

我们尽量不调用 LLM 来做翻译，理由：成本高、不可控、出口流量限制。
默认使用 `deep-translator` 走 MyMemory（mainland 可达、免费、无需 key），
配合按段/按句分块、命中缓存、专有名词后置修正。

调用方只需 `translate_to_zh(en_text)`，失败时回退为原文。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path

from .config import SETTINGS

log = logging.getLogger(__name__)

_CACHE_DIR = (SETTINGS.cache_dir.parent / "translate_cache")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_LOCK = threading.Lock()


# 译后修正表：第三方翻译引擎对部分航天专有名词译得离谱，统一矫正。
# 这里只放"译后中文 → 标准译名"映射，不放英文 → 中文（让翻译引擎自己出中文，
# 我们再把它常见的几种错法替换掉，能覆盖大部分场景）。
_FIX_PAIRS: list[tuple[str, str]] = [
    # Golden Dome：中文官方译法是『金穹』，但 Google/MyMemory 倾向于直译成『金顶』『金圆顶』
    ("金顶 计划", "金穹计划"),
    ("金顶计划", "金穹计划"),
    ("金圆顶 计划", "金穹计划"),
    ("金圆顶计划", "金穹计划"),
    ("金色穹顶 计划", "金穹计划"),
    ("金色穹顶计划", "金穹计划"),
    ("金圆顶", "金穹"),
    ("金色穹顶", "金穹"),
    ("金穹顶", "金穹"),
    ("金顶", "金穹"),
    # Iron Dome
    ("钢铁穹顶", "铁穹"),
    ("铁穹顶", "铁穹"),
    # Space Force
    ("美国太空部队", "美国太空军"),
    ("太空部队", "美国太空军"),
    ("太空力量", "美国太空军"),
    # Artemis（神话名翻得乱）
    ("阿尔忒密斯", "阿尔忒弥斯"),
    ("阿耳忒弥斯", "阿尔忒弥斯"),
    ("阿耳忒密斯", "阿尔忒弥斯"),
    # SpaceX 系列：很多译器会把 Starship 直译成『星际飞船』
    ("星际飞船", "星舰"),
    ("Starship", "星舰"),
    ("Starlink", "星链"),
    ("Falcon 9", "猎鹰 9"),
    ("Falcon Heavy", "重型猎鹰"),
    # Blue Origin & New Glenn
    ("新格伦", "新格伦"),
    # 中文标点统一
    (" ，", "，"),
    (" 。", "。"),
    (" ：", "："),
    (" ；", "；"),
]


def _apply_glossary(zh: str) -> str:
    """对译文做最小集的术语修正。"""
    if not zh:
        return zh
    out = zh
    for bad, good in _FIX_PAIRS:
        if bad and bad != good:
            out = out.replace(bad, good)
    return out


# ---------- 缓存 ----------


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _cache_path(text: str) -> Path:
    return _CACHE_DIR / f"{_key(text)}.txt"


def _cache_get(text: str) -> str | None:
    p = _cache_path(text)
    if p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return None
    return None


def _cache_put(text: str, zh: str) -> None:
    if not zh:
        return
    try:
        _cache_path(text).write_text(zh, encoding="utf-8")
    except Exception as e:
        log.debug("translate cache write failed: %s", e)


# ---------- 单次调用 ----------


_LAST_CALL_TS = 0.0
_MIN_GAP_S = 0.25  # 礼貌限流，避免 MyMemory 触发 429


def _rate_limit() -> None:
    global _LAST_CALL_TS
    with _LOCK:
        gap = time.time() - _LAST_CALL_TS
        if gap < _MIN_GAP_S:
            time.sleep(_MIN_GAP_S - gap)
        _LAST_CALL_TS = time.time()


def _call_mymemory(chunk: str) -> str:
    """单次 MyMemory 调用。出错抛异常。"""
    from deep_translator import MyMemoryTranslator
    _rate_limit()
    z = MyMemoryTranslator(source="en-US", target="zh-CN").translate(chunk)
    return (z or "").strip()


def _call_google(chunk: str) -> str:
    """GoogleTranslator —— 在能访问 translate.googleapis.com 的环境下作为备选。"""
    from deep_translator import GoogleTranslator
    _rate_limit()
    z = GoogleTranslator(source="en", target="zh-CN").translate(chunk)
    return (z or "").strip()


# ---------- 分块 ----------

_SENT_SPLIT = re.compile(r"(?<=[\.\!\?])\s+|(?<=[。！？])\s*")
_MAX_CHUNK = 480  # MyMemory 单次稳定上限大约在 500 字符


def _split_chunks(text: str, max_len: int = _MAX_CHUNK) -> list[str]:
    """按句号切，再按长度合并/硬切。"""
    if len(text) <= max_len:
        return [text]
    sents = [s for s in _SENT_SPLIT.split(text) if s and s.strip()]
    chunks: list[str] = []
    buf = ""
    for s in sents:
        if len(s) > max_len:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(s), max_len):
                chunks.append(s[i:i + max_len])
            continue
        joiner = " " if buf and not buf.endswith(("\n", " ")) else ""
        if len(buf) + len(joiner) + len(s) > max_len:
            chunks.append(buf)
            buf = s
        else:
            buf = buf + joiner + s
    if buf:
        chunks.append(buf)
    return chunks


# ---------- 主入口 ----------


_USE_GOOGLE = os.getenv("TRANSLATE_USE_GOOGLE", "0").strip() == "1"


def translate_to_zh(en: str) -> str:
    """把英文段落（或多段，以 \\n\\n 分隔）翻译成简体中文。

    - 段落级独立翻译（保留 \\n\\n）
    - 句子级软分块（≤480 字符）以适配 MyMemory 单次大小
    - 命中本地缓存直接返回
    - 失败时返回原文，调用方决定是否展示
    - 译后做最小集的术语修正（"金顶" → "金穹" 等）
    """
    if not en or not en.strip():
        return ""

    cached = _cache_get(en)
    if cached is not None:
        return cached

    paras = en.split("\n\n")
    zh_paras: list[str] = []
    failed = 0
    total_chunks = 0
    for p in paras:
        if not p.strip():
            zh_paras.append("")
            continue
        chunks = _split_chunks(p)
        zh_parts: list[str] = []
        for c in chunks:
            total_chunks += 1
            cz = _cache_get(c)
            if cz is not None:
                zh_parts.append(cz)
                continue
            translated = ""
            for fn in ((_call_google, _call_mymemory) if _USE_GOOGLE else (_call_mymemory,)):
                try:
                    translated = fn(c)
                    if translated:
                        break
                except Exception as e:
                    log.warning("translator %s failed: %s", fn.__name__, e)
                    continue
            if translated:
                _cache_put(c, translated)
                zh_parts.append(translated)
            else:
                failed += 1
                zh_parts.append(c)  # 兜底用原文
        zh_paras.append("".join(zh_parts))

    zh = _apply_glossary("\n\n".join(zh_paras))
    if failed == 0:
        _cache_put(en, zh)
    if failed and failed == total_chunks:
        log.warning("translate fully failed (%d/%d chunks), returning original text", failed, total_chunks)
    elif failed:
        log.info("translate partial failure: %d/%d chunks dropped to original", failed, total_chunks)
    return zh
