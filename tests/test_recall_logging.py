from astrbot_plugin_livingmemory.core.utils.recall_logging import (
    RecallAssemblyStage,
    RecallInjectionStage,
    RecallQueryStage,
    RecallSearchStage,
    RecentBlockStats,
    format_assembly_stage,
    format_injection_stage,
    format_query_stage,
    format_recall_stage,
    preview_query,
)


def test_query_preview_flattens_whitespace_and_only_marks_real_truncation():
    assert preview_query("  第一行\n第二行  ") == "第一行 第二行"
    assert preview_query("甲" * 80) == "甲" * 80
    assert preview_query("甲" * 81) == "甲" * 80 + "…"


def test_query_stage_reports_expansion_and_enabled_optional_controls():
    text = format_query_stage(
        RecallQueryStage(
            query="当前消息",
            expanded_query="旧消息 | 当前消息",
            expanded_history_count=1,
            top_k=5,
            importance_grace_enabled=True,
            min_importance=0.2,
            recent_enabled=True,
            recent_start_rank=3,
            recent_parent_count=2,
            recent_window_hours=48,
        )
    )

    assert text.startswith('[记忆召回·查询] 原消息="当前消息"')
    assert '实际查询="旧消息 | 当前消息"（扩展历史 1 条）' in text
    assert "top_k=5" in text
    assert "重要性宽容=开" in text
    assert "全局重要性下限=0.2" in text
    assert "recent=从第 3 个 parent 起取 2 个（48 小时内）" in text


def test_recall_stage_keeps_only_the_meaningful_top_k_denominator():
    text = format_recall_stage(
        RecallSearchStage(
            hit_count=3,
            top_k=5,
            elapsed_ms=42.4,
            importance_grace_enabled=True,
            grace_admitted_count=1,
        )
    )

    assert text == (
        "[记忆召回·召回] 本轮命中 3/5 条，其中重要性宽容准入 1 条；"
        "检索耗时 42 ms。"
    )


def test_assembly_stage_explains_continuity_recent_and_each_drop_reason():
    text = format_assembly_stage(
        RecallAssemblyStage(
            continuity_enabled=True,
            previous_loaded=2,
            older_loaded=1,
            invalid_continuity_count=1,
            repeated_count=1,
            previous_continuation=1,
            older_continuation=1,
            recent=RecentBlockStats(
                enabled=True,
                status="ok",
                parent_count=2,
                summary_count=2,
                fact_candidate_count=4,
                fact_selected_count=1,
            ),
            candidate_count=8,
            packed_count=6,
            dropped_reasons={
                "duplicate_recent_summary": 1,
                "total_budget": 1,
            },
            elapsed_ms=7.6,
        )
    )

    assert "旧槽读取 2/2 + 1/1 条" in text
    assert "canonical 重读剔除 1 条" in text
    assert "其中 1 条被本轮重新命中" in text
    assert "实际续带 1+1 条" in text
    assert "recent 选中 2 个 parent，提供 2 条摘要、1 条事实" in text
    assert "合并候选 8 条，预算保留 6 条，丢弃 2 条" in text
    assert "recent 摘要重复 1 条、总预算截断 1 条" in text
    assert text.endswith("装配耗时 8 ms。")


def test_injection_stage_uses_absolute_count_and_reports_natural_two_plus_one():
    text = format_injection_stage(
        RecallInjectionStage(
            injected=True,
            method="extra_user_content",
            outcome="injected",
            total_count=7,
            current_count=5,
            previous_count=1,
            older_count=1,
            recent_summary_count=0,
            recent_fact_count=0,
            token_count=583,
            token_budget=1600,
            next_previous_count=2,
            next_older_count=1,
            total_elapsed_ms=58.2,
            importance_grace_enabled=True,
            current_grace_count=1,
        )
    )

    assert "注入 7 条" in text
    assert "注入 7/" not in text
    assert "本轮 5（宽容准入 1）" in text
    assert "预算使用 583/1600 token（36%）" in text
    assert "下轮预存 2/2 + 1/1 条" in text


def test_empty_injection_has_no_fake_stage_denominator():
    text = format_injection_stage(
        RecallInjectionStage(
            injected=False,
            method="extra_user_content",
            outcome="no_candidates",
            total_count=0,
            current_count=0,
            previous_count=0,
            older_count=0,
            recent_summary_count=0,
            recent_fact_count=0,
            token_count=0,
            token_budget=1600,
            next_previous_count=0,
            next_older_count=0,
            total_elapsed_ms=3.2,
        )
    )

    assert text == (
        "[记忆召回·注入] 无可注入记忆；下轮预存 0/2 + 0/1 条，"
        "总耗时 3 ms。"
    )


def test_method_budget_empty_reports_every_additional_drop():
    text = format_injection_stage(
        RecallInjectionStage(
            injected=False,
            method="fake_tool_call",
            outcome="method_budget_empty",
            total_count=0,
            current_count=0,
            previous_count=0,
            older_count=0,
            recent_summary_count=0,
            recent_fact_count=0,
            token_count=0,
            token_budget=128,
            next_previous_count=0,
            next_older_count=0,
            total_elapsed_ms=9.0,
            method_budget_dropped=3,
        )
    )

    assert "注入载荷超出预算，未注入记忆" in text
    assert "注入阶段追加丢弃 3 条" in text
