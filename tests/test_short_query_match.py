"""Focused regressions for conservative short Chinese phrase matching."""

import pytest

from astrbot_plugin_livingmemory.core.utils.short_query_match import (
    is_weak_short_cjk_query,
    short_cjk_phrase_score,
    short_cjk_query_core,
)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("想见你了", "想见你"),
        ("我想见你呀", "想见你"),
        ("我的咖啡呢", "咖啡"),
        ("我又想吃火锅了", "想吃火锅"),
        ("还去爬山吗", "去爬山"),
        ("真不吃香菜啊", "不吃香菜"),
        ("你反抗不了", "你反抗不了"),
        ("还记得蓝雨伞吗", "蓝雨伞"),
        ("我还记得蓝雨伞吗", "蓝雨伞"),
        ("中午了", ""),
        ("你好啊", ""),
        ("吃了一根玉米", ""),
        ("这个呢", ""),
        ("怎么了", ""),
        ("晚安了", ""),
        ("这是一条明显超过短句范围的查询", ""),
    ],
)
def test_short_cjk_query_core_is_narrow(query, expected):
    assert short_cjk_query_core(query) == expected


def test_short_phrase_score_requires_contiguous_wording():
    assert short_cjk_phrase_score("想见你了", "昨晚说过好想见你") >= 0.8
    assert short_cjk_phrase_score("想见你了", "你已经把照片整理好了") == 0.0


@pytest.mark.parametrize(
    "query",
    ["还款计划", "真皮沙发", "又名阿团", "还原设置", "就医记录"],
)
def test_lexicalized_prefixes_are_not_stripped_as_discourse_particles(query):
    assert short_cjk_query_core(query) == query


@pytest.mark.parametrize(
    "query",
    [
        "这个呢",
        "那个？",
        "怎么了",
        "什么事",
        "哪里啊",
        "哪个呀",
        "你还记得吗",
        "中午了",
        "现在呢",
        "你好啊",
        "在呢",
    ],
)
def test_weak_standalone_short_queries_are_recognized(query):
    assert is_weak_short_cjk_query(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "这个版本呢",
        "那个蓝雨伞",
        "吃什么",
        "哪里见面",
        "哪个车站",
        "你还记得蓝雨伞吗",
        "中午吃什么",
        "现在去哪里",
        "昨天有说这个",
    ],
)
def test_content_bearing_queries_are_not_misclassified_as_weak(query):
    assert is_weak_short_cjk_query(query) is False
