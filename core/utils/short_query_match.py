"""Conservative character-phrase matching for short Chinese recall queries."""

from __future__ import annotations

import re


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TRAILING_PARTICLES = frozenset("了呢吗嘛吧啊呀哦噢啦呗哇诶哈")
_LEADING_POSSESSIVES = ("我们的", "咱们的", "我的")
_LEADING_PRONOUNS = ("我们", "咱们", "我")
_LEADING_RECALL_FRAMES = ("你还记得", "我还记得", "还记得")
_LEADING_DISCOURSE_RE = re.compile(
    r"^[又还也真就](?=[想要去来吃喝看听买卖拿放找做睡叫说问聊用玩不别喜欢爱讨厌])"
)
_WEAK_COREFERENCE_PHRASES = frozenset(
    {
        "这个",
        "那个",
        "这些",
        "那些",
        "这样",
        "那样",
        "什么",
        "怎么",
        "哪里",
        "哪个",
        "多少",
        "时候",
        "事情",
        "什么事",
        "啥事",
        "东西",
        "知道",
        "记得",
        "还记得",
        "你还记得",
        "我还记得",
        "觉得",
        "可以",
        "哈哈",
        "好的",
        "谢谢",
        "谢了",
        "晚安",
        "早安",
        "再见",
        "拜拜",
        "在吗",
        "在呢",
        "好啊",
        "咋了",
        "你好",
        "您好",
        "哈喽",
        "嗨",
        "现在",
        "今天",
        "昨天",
        "明天",
        "刚才",
        "今早",
        "今晚",
        "早上",
        "上午",
        "中午",
        "下午",
        "傍晚",
        "晚上",
        "深夜",
        "凌晨",
    }
)


def normalize_cjk(text: str) -> str:
    """Keep only CJK characters so punctuation cannot split a short phrase."""
    return "".join(_CJK_RE.findall(str(text or "")))


def _strip_trailing_particles(value: str) -> str:
    while len(value) > 2 and value[-1] in _TRAILING_PARTICLES:
        # “不了”中的“了”是否定补语的一部分，不是句尾语气词。
        if value.endswith("不了"):
            break
        value = value[:-1]
    return value


def _strip_first_prefix(value: str, prefixes: tuple[str, ...]) -> str:
    for prefix in prefixes:
        if value.startswith(prefix) and len(value) - len(prefix) >= 2:
            return value[len(prefix) :]
    return value


def is_weak_short_cjk_query(text: str) -> bool:
    """Return whether a short utterance is only deixis or conversational glue."""
    value = _strip_trailing_particles(normalize_cjk(text))
    value = _strip_first_prefix(value, _LEADING_POSSESSIVES)
    value = _strip_first_prefix(value, _LEADING_PRONOUNS)
    return value in _WEAK_COREFERENCE_PHRASES


def short_cjk_query_core(text: str) -> str:
    """Return one conservative 2-5 character phrase suitable for substring lookup.

    The raw utterance may be up to eight CJK characters so safe conversational
    wrappers can be discarded first.  The final content core is capped at five.
    Weak standalone references are intentionally excluded: they need dialogue
    context, not substring matching against long-term memory.
    """
    value = normalize_cjk(text)
    if not 2 <= len(value) <= 8:
        return ""

    value = _strip_trailing_particles(value)
    if is_weak_short_cjk_query(value):
        return ""

    value = _strip_first_prefix(value, _LEADING_RECALL_FRAMES)
    value = _strip_first_prefix(value, _LEADING_POSSESSIVES)
    value = _strip_first_prefix(value, _LEADING_PRONOUNS)
    value = _LEADING_DISCOURSE_RE.sub("", value, count=1)

    if not 2 <= len(value) <= 5 or value in _WEAK_COREFERENCE_PHRASES:
        return ""
    return value


def short_cjk_phrase_score(query: str, candidate: str) -> float:
    """Score an exact short CJK phrase without relaxing global vector thresholds."""
    core = short_cjk_query_core(query)
    if not core or core not in normalize_cjk(candidate):
        return 0.0
    return min(1.0, 0.82 + 0.04 * max(0, len(core) - 2))


__all__ = [
    "is_weak_short_cjk_query",
    "normalize_cjk",
    "short_cjk_phrase_score",
    "short_cjk_query_core",
]
