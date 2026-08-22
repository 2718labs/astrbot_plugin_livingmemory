"""LLM memory-output parsing and admission helpers."""

import json
import re
from typing import Any

from astrbot.api import logger

from ..models.memory_processing import InvalidMemoryOutputError
from ..utils.memory_facts import unique_strings


class MemoryProcessorParseMixin:
    """Parse automatic summaries and project admitted facts."""

    MAX_MEMORY_UNITS = 5
    MAX_FACTS_PER_MEMORY = 5
    MAX_FACTS_PER_WINDOW = 5

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
        if set(data) != {"memories"}:
            extras = sorted(set(data).difference({"memories"}))
            if "memory_action" in extras:
                raise InvalidMemoryOutputError("不得输出顶层 memory_action")
            detail = f": {', '.join(extras)}" if extras else ""
            raise InvalidMemoryOutputError(
                f'顶层只能包含 "memories"{detail}'
            )
        raw_units = data["memories"]
        if not isinstance(raw_units, list):
            raise InvalidMemoryOutputError("memories 必须是数组")
        if len(raw_units) > self.MAX_MEMORY_UNITS:
            raise InvalidMemoryOutputError(
                f"输出过碎：memories 最多允许 {self.MAX_MEMORY_UNITS} 条"
            )
        total_facts = sum(
            len(item.get("key_facts") or [])
            for item in raw_units
            if isinstance(item, dict) and isinstance(item.get("key_facts"), list)
        )
        if total_facts > self.MAX_FACTS_PER_WINDOW:
            raise InvalidMemoryOutputError(
                f"输出过碎：单个窗口总 fact 最多允许 {self.MAX_FACTS_PER_WINDOW} 条，"
                f"实际 {total_facts} 条"
            )
        return {
            "memories": [
                self._validate_memory_unit(item, index, is_group_chat)
                for index, item in enumerate(raw_units)
            ]
        }

    def _validate_memory_unit(
        self, item: Any, unit_index: int, is_group_chat: bool
    ) -> dict[str, Any]:
        label = f"memories[{unit_index}]"
        if not isinstance(item, dict):
            raise InvalidMemoryOutputError(f"{label} 必须是 object")
        required = {"key_facts"}
        missing = sorted(required.difference(item))
        if missing:
            raise InvalidMemoryOutputError(
                f"{label} 缺少字段: {', '.join(missing)}"
            )
        raw_facts = item["key_facts"]
        if not isinstance(raw_facts, list):
            raise InvalidMemoryOutputError(f"{label}.key_facts 必须是数组")
        if len(raw_facts) > self.MAX_FACTS_PER_MEMORY:
            raise InvalidMemoryOutputError(
                f"输出过碎：{label}.key_facts 最多允许 "
                f"{self.MAX_FACTS_PER_MEMORY} 条"
            )
        facts = [
            self._validate_candidate_fact(fact, unit_index, fact_index)
            for fact_index, fact in enumerate(raw_facts)
        ]
        return {"key_facts": facts}

    def _validate_candidate_fact(
        self, item: Any, unit_index: int, fact_index: int
    ) -> dict[str, Any]:
        label = f"memories[{unit_index}].key_facts[{fact_index}]"
        if not isinstance(item, dict):
            raise InvalidMemoryOutputError(f"{label} 必须是 object")
        missing = {"fact", "topics", "importance"}.difference(item)
        if missing:
            raise InvalidMemoryOutputError(
                f"{label} 缺少字段: {', '.join(sorted(missing))}"
            )

        fact = item["fact"]
        if not isinstance(fact, str) or not fact.strip():
            raise InvalidMemoryOutputError(f"{label}.fact 必须是非空字符串")
        importance = self._strict_importance(
            item["importance"], f"{label}.importance"
        )
        topics = self._strict_string_list(
            item["topics"], f"{label}.topics", max_items=5
        )
        reaction = self._validate_persona_reaction(
            item.get("persona_reaction"), f"{label}.persona_reaction"
        )

        # Accept and discard fields emitted by an older custom prompt. The
        # canonical contract keeps only values the model must actually judge.
        return {
            "fact": fact.strip(),
            "topics": topics,
            "importance": importance,
            "persona_reaction": reaction,
            "_legacy_skip": item.get("action") == "skip",
        }

    @staticmethod
    def _validate_persona_reaction(
        value: Any, label: str
    ) -> dict[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"emotion", "thought"}:
            raise InvalidMemoryOutputError(
                f"{label} 必须是只含 emotion/thought 的 object 或 null"
            )
        reaction: dict[str, str] = {}
        for field in ("emotion", "thought"):
            item = value[field]
            if not isinstance(item, str):
                raise InvalidMemoryOutputError(f"{label}.{field} 必须是字符串")
            text = item.strip()
            if len(text) > 80:
                raise InvalidMemoryOutputError(f"{label}.{field} 最多 80 个字符")
            reaction[field] = text
        return reaction if any(reaction.values()) else None

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

    def _prepare_admitted_units(
        self, structured_data: dict[str, Any]
    ) -> tuple[list[tuple[int, dict[str, Any]]], int, int]:
        """Filter candidate facts while retaining their single-centre unit."""
        admitted_units: list[tuple[int, dict[str, Any]]] = []
        stored_count = 0
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

        for unit_index, unit in enumerate(structured_data["memories"]):
            stored_facts: list[dict[str, Any]] = []
            for candidate in unit["key_facts"]:
                if candidate.pop("_legacy_skip", False) or candidate["importance"] <= 0.2:
                    skipped_count += 1
                    continue
                fact = candidate["fact"]
                if fact in invalid_terms or any(
                    term in fact for term in generic_subjects
                ):
                    raise InvalidMemoryOutputError(f"store fact 内容不合格: {fact}")
                if fact.startswith(("她", "他", "他们", "她们", "那个", "这件事", "后来")):
                    raise InvalidMemoryOutputError(f"store fact 缺少独立主体: {fact}")
                stored_facts.append(candidate)
            if not stored_facts:
                continue
            admitted = {
                "summary": stored_facts[0]["fact"],
                "topics": unique_strings(
                    topic for candidate in stored_facts for topic in candidate["topics"]
                ),
                "key_facts": stored_facts,
                "sentiment": "neutral",
                "importance": max(
                    candidate["importance"] for candidate in stored_facts
                ),
            }
            admitted_units.append((unit_index, admitted))
            stored_count += len(stored_facts)

        return admitted_units, stored_count, skipped_count

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
