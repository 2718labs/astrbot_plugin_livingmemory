"""
统计信息处理模块
"""

from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from .utils import PageApiUtils


class StatsHandler:
    """统计信息处理器"""

    def __init__(self, utils: "PageApiUtils"):
        """
        初始化统计处理器

        Args:
            utils: PageApiUtils 工具实例
        """
        self.utils = utils

    async def get_stats(self, memory_engine) -> dict[str, Any]:
        """
        获取插件统计信息

        包括：
        - 记忆总数、会话统计
        - 图谱节点、边、入口统计
        - 原子统计
        - 重要性分布
        - 最近会话列表

        Args:
            memory_engine: 记忆引擎实例

        Returns:
            包含统计信息的字典
        """
        try:
            stats = await memory_engine.get_statistics()

            # 使用专用的 COUNT(*) 统计，确保显示完整图谱总数
            graph_store = self.utils.get_graph_store(memory_engine)
            if graph_store is not None:
                try:
                    entry_stats = await graph_store.get_memory_entry_stats()
                    stats["graph_nodes"] = entry_stats.get("graph_nodes", 0)
                    stats["graph_edges"] = entry_stats.get("graph_edges", 0)
                    stats["graph_entries"] = entry_stats.get("graph_entries", 0)
                except Exception:
                    stats["graph_nodes"] = 0
                    stats["graph_edges"] = 0
                    stats["graph_entries"] = 0
            else:
                stats["graph_nodes"] = 0
                stats["graph_edges"] = 0
                stats["graph_entries"] = 0

            # S2/S4: canonical facts are the production memory unit.  Keep
            # document totals for parent-level management, but expose the
            # authoritative fact population and its searchable projections.
            stats["canonical_parent_count"] = 0
            stats["canonical_fact_count"] = 0
            stats["canonical_fact_breakdown"] = {}
            stats["canonical_index_status"] = {
                "consistent": True,
                "facts": 0,
                "fts": 0,
                "vectors": 0,
            }
            canonical_store = getattr(memory_engine, "canonical_store", None)
            canonical_db = getattr(canonical_store, "db", None)
            if canonical_db is not None:
                try:
                    cursor = await canonical_db.execute(
                        "SELECT COUNT(*) AS count FROM memory_parents"
                    )
                    row = await cursor.fetchone()
                    stats["canonical_parent_count"] = int(row["count"] if row else 0)
                    cursor = await canonical_db.execute(
                        "SELECT status, COUNT(*) AS count "
                        "FROM memory_facts GROUP BY status"
                    )
                    breakdown = {
                        str(row["status"]): int(row["count"])
                        for row in await cursor.fetchall()
                    }
                    stats["canonical_fact_breakdown"] = breakdown
                    stats["canonical_fact_count"] = sum(breakdown.values())
                    index_status = await canonical_store.index_status()
                    stats["canonical_index_status"] = {
                        "consistent": index_status.is_consistent,
                        "facts": index_status.fact_count,
                        "fts": index_status.fts_count,
                        "vectors": index_status.vector_count,
                    }
                except Exception:
                    logger.warning(
                        "[PageAPI] 获取 canonical fact 统计失败", exc_info=True
                    )

            # 原子统计 (if available)
            atom_store = getattr(memory_engine, "atom_store", None)
            stats["atom_count"] = 0
            stats["atom_breakdown"] = {}
            stats["atom_retired"] = True
            if atom_store is not None:
                try:
                    stats["atom_count"] = await atom_store.count_atoms() or 0
                except Exception:
                    pass
                try:
                    stats["atom_breakdown"] = await atom_store.count_by_type()
                except Exception:
                    pass

            # 重要性分布 — 兜底默认值（get_statistics 已计算，此处仅容错）
            if "importance_distribution" not in stats:
                stats["importance_distribution"] = {
                    "0-1": 0,
                    "1-2": 0,
                    "2-3": 0,
                    "3-4": 0,
                    "4-5": 0,
                    "5-6": 0,
                    "6-7": 0,
                    "7-8": 0,
                    "8-9": 0,
                    "9-10": 0,
                }

            # 最近会话从 sessions 统计数据派生
            session_data = stats.get("sessions", {})
            stats["recent_sessions"] = (
                [
                    {"session_id": sid, "message_count": cnt}
                    for sid, cnt in sorted(session_data.items(), key=lambda x: -x[1])[
                        :10
                    ]
                ]
                if isinstance(session_data, dict)
                else []
            )

            return self.utils.ok(stats)
        except Exception as exc:
            logger.error(f"[PageAPI] 获取统计信息失败: {exc}", exc_info=True)
            return self.utils.error(str(exc))
