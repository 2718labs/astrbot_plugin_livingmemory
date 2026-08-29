"""Deterministic, side-effect-free formatting for automatic recall logs."""

from __future__ import annotations

from dataclasses import dataclass, field


DROP_REASON_LABELS = {
    "duplicate_recent_summary": "recent 摘要重复",
    "duplicate_or_empty": "重复或空内容",
    "single_fact_budget": "单条超限",
    "total_budget": "总预算截断",
    "baseline_duplicate": "与用户画像重复",
}


def preview_query(value: str, *, limit: int = 80) -> str:
    """Flatten a query for one-line logs and add an ellipsis only if truncated."""
    normalized = " ".join(str(value or "").split()).replace('"', '\\"')
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "…"


@dataclass(slots=True)
class RecallQueryStage:
    query: str
    top_k: int
    expanded_query: str | None = None
    expanded_history_count: int = 0
    skip_reason: str | None = None
    importance_grace_enabled: bool = False
    min_importance: float = 0.0
    recent_enabled: bool = False
    recent_start_rank: int = 1
    recent_parent_count: int = 1
    recent_window_hours: float = 48.0


@dataclass(slots=True)
class RecallSearchStage:
    hit_count: int
    top_k: int
    elapsed_ms: float
    importance_grace_enabled: bool = False
    grace_admitted_count: int = 0


@dataclass(slots=True)
class RecentBlockStats:
    enabled: bool = False
    status: str = "disabled"
    parent_count: int = 0
    summary_count: int = 0
    fact_candidate_count: int = 0
    fact_selected_count: int = 0


@dataclass(slots=True)
class RecallAssemblyStage:
    continuity_enabled: bool
    previous_loaded: int
    older_loaded: int
    invalid_continuity_count: int
    repeated_count: int
    previous_continuation: int
    older_continuation: int
    recent: RecentBlockStats = field(default_factory=RecentBlockStats)
    candidate_count: int = 0
    packed_count: int = 0
    dropped_reasons: dict[str, int] = field(default_factory=dict)
    elapsed_ms: float = 0.0


@dataclass(slots=True)
class RecallInjectionStage:
    injected: bool
    method: str
    outcome: str
    total_count: int
    current_count: int
    previous_count: int
    older_count: int
    recent_summary_count: int
    recent_fact_count: int
    token_count: int
    token_budget: int
    next_previous_count: int
    next_older_count: int
    total_elapsed_ms: float
    continuity_enabled: bool = True
    importance_grace_enabled: bool = False
    current_grace_count: int = 0
    recent_enabled: bool = False
    method_budget_dropped: int = 0


def format_query_stage(stage: RecallQueryStage) -> str:
    expanded = str(stage.expanded_query or "").strip()
    if expanded and expanded != str(stage.query or "").strip():
        lead = (
            f'原消息="{preview_query(stage.query)}"；'
            f'实际查询="{preview_query(expanded)}"'
            f"（扩展历史 {stage.expanded_history_count} 条）"
        )
    else:
        lead = f'查询="{preview_query(stage.query)}"'

    details: list[str] = []
    if stage.skip_reason:
        details.append("轻量消息，跳过本轮检索，仅检查续带")
    else:
        details.append(f"top_k={stage.top_k}")
    if stage.importance_grace_enabled:
        details.append("重要性宽容=开")
    if stage.min_importance > 0:
        details.append(f"全局重要性下限={stage.min_importance:g}")
    if stage.recent_enabled:
        details.append(
            "recent="
            f"从第 {stage.recent_start_rank} 个 parent 起取 "
            f"{stage.recent_parent_count} 个（{stage.recent_window_hours:g} 小时内）"
        )
    return f"[记忆召回·查询] {lead}；{'，'.join(details)}。"


def format_recall_stage(stage: RecallSearchStage) -> str:
    detail = f"本轮命中 {stage.hit_count}/{stage.top_k} 条"
    if stage.importance_grace_enabled:
        detail += f"，其中重要性宽容准入 {stage.grace_admitted_count} 条"
    return f"[记忆召回·召回] {detail}；检索耗时 {stage.elapsed_ms:.0f} ms。"


def _format_recent(stats: RecentBlockStats) -> str | None:
    if not stats.enabled:
        return None
    if stats.status == "skipped":
        return "recent 未执行（本轮检索已跳过）"
    if stats.status == "no_parents":
        return "recent 在时间窗口内无可用 parent"
    if stats.status == "store_unavailable":
        return "recent 存储不可用"
    if stats.status == "error":
        return "recent 构建失败"

    detail = (
        f"recent 选中 {stats.parent_count} 个 parent，"
        f"提供 {stats.summary_count} 条摘要、{stats.fact_selected_count} 条事实"
        f"（事实候选 {stats.fact_candidate_count} 条）"
    )
    if stats.status == "facts_unavailable":
        detail += "，事实读取不可用"
    return detail


def _format_drop_reasons(reasons: dict[str, int]) -> str:
    return "、".join(
        f"{DROP_REASON_LABELS.get(reason, reason)} {count} 条"
        for reason, count in reasons.items()
        if count > 0
    )


def format_assembly_stage(stage: RecallAssemblyStage) -> str:
    sections: list[str] = []
    if stage.continuity_enabled:
        continuity = (
            f"旧槽读取 {stage.previous_loaded}/2 + {stage.older_loaded}/1 条"
        )
        if stage.invalid_continuity_count:
            continuity += f"，canonical 重读剔除 {stage.invalid_continuity_count} 条"
        if stage.repeated_count:
            continuity += f"，其中 {stage.repeated_count} 条被本轮重新命中"
        continuity += (
            f"，实际续带 {stage.previous_continuation}+"
            f"{stage.older_continuation} 条"
        )
        sections.append(continuity)
    else:
        sections.append("续带关闭")

    recent_text = _format_recent(stage.recent)
    if recent_text:
        sections.append(recent_text)

    packing = (
        f"合并候选 {stage.candidate_count} 条，预算保留 {stage.packed_count} 条"
    )
    dropped_count = sum(max(0, count) for count in stage.dropped_reasons.values())
    if dropped_count:
        packing += f"，丢弃 {dropped_count} 条"
        details = _format_drop_reasons(stage.dropped_reasons)
        if details:
            packing += f"（{details}）"
    else:
        packing += "，无丢弃"
    sections.append(packing)
    sections.append(f"装配耗时 {stage.elapsed_ms:.0f} ms")
    return f"[记忆召回·装配] {'；'.join(sections)}。"


def _budget_text(token_count: int, token_budget: int) -> str:
    percentage = 0
    if token_budget > 0:
        percentage = int(token_count * 100 / token_budget + 0.5)
    return f"预算使用 {token_count}/{token_budget} token（{percentage}%）"


def format_injection_stage(stage: RecallInjectionStage) -> str:
    if not stage.injected:
        outcomes = {
            "no_candidates": "无可注入记忆",
            "packing_empty": "候选均未通过注入预算，未注入记忆",
            "method_budget_empty": "注入载荷超出预算，未注入记忆",
        }
        result = outcomes.get(stage.outcome, "未注入记忆")
        suffixes: list[str] = []
        if stage.method_budget_dropped:
            suffixes.append(f"注入阶段追加丢弃 {stage.method_budget_dropped} 条")
        if stage.continuity_enabled:
            suffixes.append("下轮预存 0/2 + 0/1 条")
        suffixes.append(f"总耗时 {stage.total_elapsed_ms:.0f} ms")
        return f"[记忆召回·注入] {result}；{'，'.join(suffixes)}。"

    current = f"本轮 {stage.current_count}"
    if stage.importance_grace_enabled:
        current += f"（宽容准入 {stage.current_grace_count}）"
    sources = [
        current,
        f"上轮 {stage.previous_count}",
        f"上上轮 {stage.older_count}",
    ]
    if stage.recent_enabled:
        sources.extend(
            [
                f"recent 摘要 {stage.recent_summary_count}",
                f"recent 事实 {stage.recent_fact_count}",
            ]
        )

    sections = [
        f"通过 {stage.method} 注入 {stage.total_count} 条：{'、'.join(sources)}"
    ]
    if stage.method_budget_dropped:
        sections.append(f"注入阶段追加丢弃 {stage.method_budget_dropped} 条")
    sections.append(_budget_text(stage.token_count, stage.token_budget))
    if stage.continuity_enabled:
        sections.append(
            f"下轮预存 {stage.next_previous_count}/2 + "
            f"{stage.next_older_count}/1 条"
        )
    sections.append(f"总耗时 {stage.total_elapsed_ms:.0f} ms")
    return f"[记忆召回·注入] {'；'.join(sections)}。"


__all__ = [
    "RecallAssemblyStage",
    "RecallInjectionStage",
    "RecallQueryStage",
    "RecallSearchStage",
    "RecentBlockStats",
    "format_assembly_stage",
    "format_injection_stage",
    "format_query_stage",
    "format_recall_stage",
    "preview_query",
]
