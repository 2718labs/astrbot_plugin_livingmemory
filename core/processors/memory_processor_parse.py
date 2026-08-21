"""LLM memory-output parsing and admission helpers."""

import json
import re
from typing import Any

from astrbot.api import logger

from ..models.memory_processing import InvalidMemoryOutputError


class MemoryProcessorParseMixin:
    """Parse automatic summaries and project admitted facts."""

    def _parse_llm_response(
        self, response_text: str, is_group_chat: bool
    ) -> dict[str, Any]:
        """Decode and strictly validate an automatic-summary response."""
        logger.debug(f"[MemoryProcessor] 开始严格校验 LLM 响应，长度={len(response_text)}")
        try:
            cleaned_text = self._strip_json_fence(response_text)
            data = json.loads(cleaned_text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise InvalidMemoryOutputError(f"JSON 解析失败: {exc}") from exc
        return self._validate_llm_payload(data, is_group_chat)

    @staticmethod
    def _strip_json_fence(response_text: str) -> str:
        """Remove one complete Markdown fence without extracting fragments."""
        cleaned = response_text.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
        if fenced:
            return fenced.group(1).strip()
        if cleaned.startswith("```") or cleaned.endswith("```"):
            raise InvalidMemoryOutputError("Markdown JSON 代码围栏不完整")
        return cleaned

    def _validate_llm_payload(
        self, data: Any, is_group_chat: bool
    ) -> dict[str, Any]:
        """Validate the raw schema without filling missing required fields."""
        if not isinstance(data, dict):
            raise InvalidMemoryOutputError("顶层必须是 JSON object")
        if "memory_action" in data:
            raise InvalidMemoryOutputError("不得输出顶层 memory_action")

        required = {"summary", "topics", "key_facts", "sentiment", "importance"}
        if is_group_chat:
            required.add("participants")
        missing = sorted(required.difference(data))
        if missing:
            raise InvalidMemoryOutputError(f"缺少必填字段: {', '.join(missing)}")

        summary = data["summary"]
        if not isinstance(summary, str):
            raise InvalidMemoryOutputError("summary 必须是字符串")

        topics = self._strict_string_list(data["topics"], "topics", max_items=5)
        sentiment = data["sentiment"]
        if sentiment not in {"positive", "neutral", "negative"}:
            raise InvalidMemoryOutputError(
                "sentiment 必须是 positive、neutral 或 negative"
            )
        importance = self._strict_importance(data["importance"], "importance")

        raw_facts = data["key_facts"]
        if not isinstance(raw_facts, list):
            raise InvalidMemoryOutputError("key_facts 必须是数组")
        if len(raw_facts) > 5:
            raise InvalidMemoryOutputError("key_facts 最多允许 5 条")
        facts = [
            self._validate_candidate_fact(item, index)
            for index, item in enumerate(raw_facts)
        ]

        normalized: dict[str, Any] = {
            **data,
            "summary": summary.strip(),
            "topics": topics,
            "key_facts": facts,
            "sentiment": sentiment,
            "importance": importance,
        }
        if "canonical_summary" in data:
            if not isinstance(data["canonical_summary"], str):
                raise InvalidMemoryOutputError("canonical_summary 必须是字符串")
            normalized["canonical_summary"] = data["canonical_summary"].strip()
        if is_group_chat:
            normalized["participants"] = self._strict_string_list(
                data["participants"], "participants"
            )
        return normalized

    def _validate_candidate_fact(self, item: Any, index: int) -> dict[str, Any]:
        label = f"key_facts[{index}]"
        if not isinstance(item, dict):
            raise InvalidMemoryOutputError(f"{label} 必须是 object")
        missing = {"fact", "action", "importance"}.difference(item)
        if missing:
            raise InvalidMemoryOutputError(
                f"{label} 缺少字段: {', '.join(sorted(missing))}"
            )

        fact = item["fact"]
        if not isinstance(fact, str) or not fact.strip():
            raise InvalidMemoryOutputError(f"{label}.fact 必须是非空字符串")
        action = item["action"]
        if action not in {"store", "skip"}:
            raise InvalidMemoryOutputError(f"{label}.action 只能是 store 或 skip")
        importance = self._strict_importance(
            item["importance"], f"{label}.importance"
        )
        if "reason" in item and not isinstance(item["reason"], str):
            raise InvalidMemoryOutputError(f"{label}.reason 必须是字符串")

        normalized = dict(item)
        normalized["fact"] = fact.strip()
        normalized["action"] = action
        normalized["importance"] = importance
        if "reason" in normalized:
            normalized["reason"] = normalized["reason"].strip()
        return normalized

    @staticmethod
    def _strict_string_list(
        value: Any, field: str, max_items: int | None = None
    ) -> list[str]:
        if not isinstance(value, list):
            raise InvalidMemoryOutputError(f"{field} 必须是字符串数组")
        if max_items is not None and len(value) > max_items:
            raise InvalidMemoryOutputError(f"{field} 最多允许 {max_items} 项")
        result: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                raise InvalidMemoryOutputError(
                    f"{field}[{index}] 必须是非空字符串"
                )
            result.append(item.strip())
        return result

    @staticmethod
    def _strict_importance(value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidMemoryOutputError(f"{field} 必须是 0.0 到 1.0 的数字")
        importance = float(value)
        if not 0.0 <= importance <= 1.0:
            raise InvalidMemoryOutputError(f"{field} 必须在 0.0 到 1.0 之间")
        return importance

    def _prepare_admitted_projection(
        self, structured_data: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, int, int]:
        """Filter candidate facts and project admitted text to current consumers."""
        stored_facts: list[dict[str, Any]] = []
        skipped_count = 0
        invalid_terms = {
            "对话记录",
            "聊天记录",
            "无实质内容",
            "没有重要内容",
        }
        generic_subjects = (
            "某用户",
            "某人",
            "有人",
            "用户说",
            "对方说",
            "群成员说",
        )

        for candidate in structured_data["key_facts"]:
            if candidate["action"] == "skip" or candidate["importance"] <= 0.2:
                skipped_count += 1
                continue
            fact = candidate["fact"]
            if fact in invalid_terms or any(term in fact for term in generic_subjects):
                raise InvalidMemoryOutputError(f"store fact 内容不合格: {fact}")
            stored_facts.append(candidate)

        if not stored_facts:
            return None, 0, skipped_count

        fact_texts = [item["fact"] for item in stored_facts]
        projected = dict(structured_data)
        projected["summary"] = fact_texts[0]
        projected["canonical_summary"] = "；".join(fact_texts)
        projected["key_facts"] = fact_texts
        projected["importance"] = max(item["importance"] for item in stored_facts)
        return projected, len(stored_facts), skipped_count

    def _normalize_parsed_data(self, data: dict, is_group_chat: bool) -> dict[str, Any]:
        """
        规范化解析后的数据（补充缺失字段、类型转换）

        Args:
            data: 解析后的原始字典
            is_group_chat: 是否为群聊

        Returns:
            规范化后的字典
        """
        required_fields = ["summary", "topics", "key_facts", "sentiment", "importance"]
        if is_group_chat:
            required_fields.append("participants")

        for field in required_fields:
            if field not in data:
                data[field] = self._get_default_value(field)

        data["summary"] = str(data.get("summary", ""))
        data["canonical_summary"] = str(data.get("canonical_summary") or "").strip()
        data["topics"] = self._ensure_list(data.get("topics", []))[:5]
        data["key_facts"] = self._ensure_list(data.get("key_facts", []))[:5]
        data["sentiment"] = self._validate_sentiment(data.get("sentiment", "neutral"))
        data["importance"] = self._validate_importance(data.get("importance", 0.5))

        if is_group_chat:
            data["participants"] = self._ensure_list(data.get("participants", []))

        return data

    def _ensure_list(self, value: Any) -> list[str]:
        """确保值是字符串列表"""
        if isinstance(value, list):
            return [str(item) for item in value if item]
        elif isinstance(value, str):
            return [value] if value else []
        else:
            return []

    def _validate_sentiment(self, sentiment: str) -> str:
        """验证情感值"""
        valid_sentiments = ["positive", "neutral", "negative"]
        sentiment = sentiment.lower()
        return sentiment if sentiment in valid_sentiments else "neutral"

    def _validate_importance(self, importance: Any) -> float:
        """验证重要性评分"""
        try:
            score = float(importance)
            return max(0.0, min(1.0, score))  # 限制在0-1之间
        except (ValueError, TypeError):
            return 0.5

    def _get_default_value(self, field: str) -> Any:
        """获取字段的默认值"""
        defaults = {
            "summary": "",
            "canonical_summary": "",
            "topics": [],
            "key_facts": [],
            "participants": [],
            "sentiment": "neutral",
            "importance": 0.5,
        }
        return defaults.get(field, "")

    def _validate_summary_quality(self, structured_data: dict[str, Any]) -> str:
        """
        校验总结质量，返回质量等级。

        检查规则：
        1. summary 不能为空或过短（< 10 字符）
        2. key_facts 至少有 1 条
        3. importance 在合法范围内
        4. summary 不含泛化词（"某用户"、"有人"等）

        Returns:
            "normal" 或 "low"
        """
        summary = structured_data.get("summary", "")
        key_facts = structured_data.get("key_facts", [])
        importance = structured_data.get("importance", 0.5)

        if not summary or len(summary.strip()) < 10:
            return "low"
        if not key_facts:
            return "low"
        if not isinstance(importance, (int, float)) or not (0.0 <= importance <= 1.0):
            return "low"

        # 泛化词检测
        generic_terms = [
            "某用户",
            "有人",
            "某人",
            "用户说",
            "对方说",
            "群成员",
            "某群成员",
        ]
        if any(term in summary for term in generic_terms):
            return "low"

        return "normal"

    def build_memory_from_structured_data(
        self,
        structured_data: dict[str, Any],
        is_group_chat: bool = False,
        fallback_excerpt: str = "",
    ) -> tuple[str, dict[str, Any], float]:
        """复用自动总结流程，将结构化数据转换为标准记忆存储格式。"""
        # 与自动总结路径保持一致：先校验质量，再规范化。
        # 这样原始 importance 越界等异常仍会被判为 low quality。
        quality = self._validate_summary_quality(structured_data)
        normalized = self._normalize_parsed_data(structured_data, is_group_chat)
        normalized["_quality"] = quality

        content, metadata = self._build_storage_format(
            fallback_excerpt or normalized.get("summary", ""),
            normalized,
            is_group_chat,
        )
        metadata["summary_quality"] = quality
        return (
            content,
            metadata,
            self._validate_importance(normalized.get("importance")),
        )
