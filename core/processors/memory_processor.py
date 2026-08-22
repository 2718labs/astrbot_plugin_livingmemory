"""
记忆处理器 - 使用LLM将对话历史处理为结构化记忆
"""

import asyncio
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..models.conversation_models import Message
from ..models.memory_processing import (
    InvalidMemoryOutputError,
    MemoryAdmissionSkipped,
    MemoryProcessingResult,
)
from .memory_processor_parse import MemoryProcessorParseMixin
from .memory_processor_build import MemoryProcessorBuildMixin


class MemoryProcessor(MemoryProcessorParseMixin, MemoryProcessorBuildMixin):
    """
    记忆处理器

    使用LLM将对话历史转换为结构化记忆。
    支持私聊和群聊两种场景的不同处理策略。
    """

    def __init__(
        self,
        context=None,
        llm_provider: Any = None,
        config: dict[str, Any] | None = None,
    ):
        """
        初始化记忆处理器

        Args:
            context: AstrBot上下文,用于获取人格管理器
            llm_provider: LLM Provider 实例或 Provider ID 字符串。
                          传入实例时直接使用（测试用）；传入字符串时动态解析。
                          留空则使用AstrBot默认Provider。
            config: 记忆处理器配置。
        """
        self.context = context
        self._llm_provider = llm_provider
        self.config = config or {}
        self._topic_catalog: dict[str, dict[str, dict[str, str]]] = {}

        # 加载提示词模板
        self._load_prompts()

    def _get_current_llm_provider(self):
        """动态解析LLM Provider以避免持有过期引用

        AstrBot可能在运行期间重新创建Provider实例（例如配置变更后），
        旧的Provider实例内部的httpx client会被关闭，导致
        RuntimeError: Cannot send a request, as the client has been closed.
        因此每次调用前都从AstrBot上下文重新获取当前有效的Provider。
        """
        if not self.context:
            # 无 context 时直接返回传入的 provider 实例（测试路径）
            if self._llm_provider is not None and not isinstance(
                self._llm_provider, str
            ):
                return self._llm_provider
            return None

        # 如果传入的是 provider 实例（非字符串），直接使用（测试路径）
        if self._llm_provider is not None and not isinstance(self._llm_provider, str):
            return self._llm_provider

        # 优先使用配置中指定的Provider ID（字符串）
        if isinstance(self._llm_provider, str) and self._llm_provider:
            try:
                provider = self.context.get_provider_by_id(self._llm_provider)
                if provider:
                    return provider
            except Exception:
                pass

        # 回退到AstrBot当前默认Provider
        try:
            provider = self.context.get_using_provider()
            if provider:
                return provider
        except Exception:
            pass

        return None

    def _load_prompts(self) -> None:
        """从 PromptManager 加载提示词模板（支持用户自定义覆盖）"""
        try:
            from ..prompts.prompt_manager import get_prompt_manager

            mgr = get_prompt_manager()
            if mgr is not None:
                self.private_chat_prompt = mgr.get_prompt("private_chat_prompt")
                self.group_chat_prompt = mgr.get_prompt("group_chat_prompt")
                logger.info("[MemoryProcessor] 通过 PromptManager 加载提示词模板成功")
            else:
                self._load_prompts_fallback()
        except Exception as e:
            logger.error(f"[MemoryProcessor] 通过 PromptManager 加载提示词失败: {e}")
            self._load_prompts_fallback()

    def _get_chat_prompt(self, is_group_chat: bool) -> str:
        """每次处理时从 PromptManager 实时读取，确保 WebUI 保存后立即生效。"""
        try:
            from ..prompts.prompt_manager import get_prompt_manager

            mgr = get_prompt_manager()
            if mgr is not None:
                prompt_id = "group_chat_prompt" if is_group_chat else "private_chat_prompt"
                return mgr.get_prompt(prompt_id)
        except Exception:
            pass
        return self.group_chat_prompt if is_group_chat else self.private_chat_prompt

    @staticmethod
    def _build_admission_output_contract(is_group_chat: bool) -> str:
        """Return the compact, non-overridable memory-output contract."""
        peer = "群成员" if is_group_chat else "对方"
        return (
            "## 输出与判断规则\n"
            '- 只输出 {"memories":[{"key_facts":[...]}]}；最多 5 条 memory，'
            '每条最多 5 个 fact，整个窗口合计最多 5 个 fact。\n'
            '- 每个 fact 只写 "fact"、"topics"、"importance"；确有价值时可加 '
            '"persona_reaction"。不要输出其他字段。\n'
            '- fact 中的“今天、昨天、明天、下周”等相对时间，按说出该时间的消息日期'
            '改写为具体日期；不要把消息发送时间本身写成事实。persona_reaction 格式为 '
            '{"emotion","thought"}。\n'
            "- 只输出值得长期接续的事实；寒暄、填充、临时报错、无后果的即时状态和重复内容直接不输出。\n"
            "- 先读到窗口结尾。后面的明确否认、纠正、澄清或形成的约定覆盖前面的说法；"
            "不得保存已被否认的版本。\n"
            f"- 只提取本窗口新确认的信息。Bot 复述的旧记忆、人格设定、单方面推测，以及未经{peer}确认的建议或旧约定，不得写入。\n"
            "- 承诺、约定、边界和偏好按普通事实保存，写清谁提出、是否接受；短期定时任务不由记忆系统代办。\n"
            "- 单次玩笑、昵称或亲昵称呼不自动成为稳定偏好；单次重要冲突、修复或共同意义仍可保存。\n"
            "- 一条 memory 只围绕一个中心；同一事件、同一段关系变化或同一结论的过程话语必须合并，"
            "不要为了覆盖每句话而拆成多条 fact。\n"
            "- 每条 fact 记录一个以后需要整体接续的事实或事件，允许包含同一事件的原因、发展与结果；"
            "topics 只属于该 fact。\n"
            f"- fact 必须中性、自包含，并使用{peer}的具体昵称。描述当前 Bot 自己时只用第一人称“我”；[Bot: ...] 不是用户。\n"
            "- importance 为 0.0 到 1.0；没有事实时输出 {\"memories\":[]}。"
        )

    def _load_prompts_fallback(self) -> None:
        """后备加载：直接从文件读取提示词"""
        prompt_dir = Path(__file__).parent.parent / "prompts"

        try:
            private_prompt_file = prompt_dir / "private_chat_prompt.txt"
            with open(private_prompt_file, encoding="utf-8") as f:
                self.private_chat_prompt = f.read()

            group_prompt_file = prompt_dir / "group_chat_prompt.txt"
            with open(group_prompt_file, encoding="utf-8") as f:
                self.group_chat_prompt = f.read()

            logger.info("[MemoryProcessor] 提示词模板加载成功（后备模式）")

        except Exception as e:
            logger.error(f"[MemoryProcessor] 加载提示词模板失败: {e}")
            self.private_chat_prompt = """从以下私聊中提取以后仍需接续的事实:
{conversation}
"""
            self.group_chat_prompt = """从以下群聊中提取以后仍需接续的事实:
{conversation}
"""

    async def _build_system_prompt_with_persona(self, persona_id: str | None) -> str:
        """
        构建包含人格提示的 system_prompt

        Args:
            persona_id: 人格ID

        Returns:
            str: 包含人格提示的 system_prompt
        """
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 尝试从 PromptManager 获取基础 system prompt
        try:
            from ..prompts.prompt_manager import get_prompt_manager

            mgr = get_prompt_manager()
            if mgr is not None:
                base_prompt = mgr.get_prompt("memory_system_prompt_base").replace(
                    "{current_date}", current_date
                )
            else:
                base_prompt = self._build_base_prompt_fallback(current_date)
        except Exception:
            base_prompt = self._build_base_prompt_fallback(current_date)

        if not persona_id:
            logger.debug("[MemoryProcessor] 未指定人格ID，使用基础提示词")
            return base_prompt

        if not self.context:
            logger.debug("[MemoryProcessor] Context 未设置，使用基础提示词")
            return base_prompt

        try:
            persona_manager = getattr(self.context, "persona_manager", None)
            if not persona_manager:
                logger.warning(
                    "[MemoryProcessor] persona_manager 不可用，使用基础提示词"
                )
                return base_prompt

            persona = await persona_manager.get_persona(persona_id)
            if not persona:
                logger.warning(
                    f"[MemoryProcessor] 人格 '{persona_id}' 不存在，使用基础提示词"
                )
                return base_prompt

            if not persona.system_prompt:
                logger.debug(
                    f"[MemoryProcessor] 人格 '{persona_id}' 无 system_prompt，使用基础提示词"
                )
                return base_prompt

            persona_prompt = persona.system_prompt.strip()
            if not persona_prompt:
                logger.debug(
                    f"[MemoryProcessor] 人格 '{persona_id}' 的 system_prompt 为空，使用基础提示词"
                )
                return base_prompt

            logger.info(
                f"[MemoryProcessor] 成功加载人格 '{persona_id}' 的提示词 "
                f"(长度={len(persona_prompt)}字符)"
            )
            logger.debug(f"[MemoryProcessor] 人格提示词预览: {persona_prompt[:100]}...")

            # 使用 PromptManager 模板构建增强提示词
            try:
                if mgr is not None:
                    enhanced_template = mgr.get_prompt(
                        "memory_system_prompt_with_persona"
                    )
                    enhanced_prompt = (
                        enhanced_template.replace("{base_prompt}", base_prompt)
                        .replace("{persona_prompt}", persona_prompt)
                        .replace("{current_date}", current_date)
                    )
                else:
                    enhanced_prompt = self._build_enhanced_prompt_fallback(
                        base_prompt, persona_prompt, current_date
                    )
            except Exception:
                enhanced_prompt = self._build_enhanced_prompt_fallback(
                    base_prompt, persona_prompt, current_date
                )

            return enhanced_prompt

        except ValueError as e:
            logger.warning(f"[MemoryProcessor] 人格 '{persona_id}' 不存在: {e}")
            return base_prompt
        except Exception as e:
            logger.error(
                f"[MemoryProcessor] 获取人格提示词时发生错误: {e}", exc_info=True
            )
            return base_prompt

    @staticmethod
    def _build_base_prompt_fallback(current_date: str) -> str:
        """后备基础 system prompt（当 PromptManager 不可用时）"""
        return f"你负责从对话窗口提取可长期接续的事实，并严格输出指定 JSON。当前日期时间：{current_date}"

    @staticmethod
    def _build_enhanced_prompt_fallback(
        base_prompt: str, persona_prompt: str, current_date: str
    ) -> str:
        """后备增强 system prompt（当 PromptManager 不可用时）"""
        return (
            f"{base_prompt}\n\n"
            f"## 当前人格\n"
            f"{persona_prompt}\n\n"
            "人格只用于理解 Bot 的身份以及可选的短句 persona_reaction，"
            "不得把人格设定本身写成新事实。"
        )

    async def _call_llm_with_retry(
        self, prompt: str, system_prompt: str, max_retries: int = 3
    ) -> str:
        """
        带指数退避的 LLM 调用

        Args:
            prompt: 提示词
            system_prompt: 系统提示词
            max_retries: 最大重试次数

        Returns:
            LLM 响应文本
        """
        last_error = None
        for attempt in range(max_retries):
            try:
                provider = self._get_current_llm_provider()
                if not provider:
                    raise RuntimeError("LLM Provider 不可用")
                response = await provider.text_chat(
                    prompt=prompt, system_prompt=system_prompt
                )
                return response.completion_text
            except Exception as e:
                last_error = e
                if attempt == max_retries - 1:
                    raise
                wait_time = (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    f"[MemoryProcessor] LLM 调用失败，{wait_time:.1f}s 后重试 "
                    f"({attempt + 1}/{max_retries}): {e}"
                )
                await asyncio.sleep(wait_time)
        if last_error:
            raise last_error
        raise RuntimeError("LLM 调用失败，未捕获到具体异常")

    def _try_fix_json(self, text: str) -> str:
        """
        尝试修复损坏的 JSON 字符串

        Args:
            text: 可能损坏的 JSON 字符串

        Returns:
            修复后的 JSON 字符串
        """
        fixed = text.strip()

        # 移除 markdown 代码块标记
        if fixed.startswith("```json"):
            fixed = fixed[7:]
        elif fixed.startswith("```"):
            fixed = fixed[3:]
        if fixed.endswith("```"):
            fixed = fixed[:-3]
        fixed = fixed.strip()

        # 修复未闭合的字符串（截断的 JSON）
        open_quotes = fixed.count('"') - fixed.count('\\"')
        if open_quotes % 2 != 0:
            fixed += '"'

        # 修复未闭合的数组
        open_brackets = fixed.count("[") - fixed.count("]")
        if open_brackets > 0:
            fixed += "]" * open_brackets

        # 修复未闭合的对象
        open_braces = fixed.count("{") - fixed.count("}")
        if open_braces > 0:
            fixed += "}" * open_braces

        # 移除尾部逗号（JSON 不允许）
        fixed = re.sub(r",(\s*[}\]])", r"\1", fixed)

        # 修复常见的转义问题
        fixed = fixed.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

        return fixed

    async def _repair_llm_response_format(
        self,
        response_text: str,
        is_group_chat: bool,
        validation_error: InvalidMemoryOutputError | None = None,
    ) -> str:
        """Ask the LLM once to repair either structure or excessive fragmentation."""
        if validation_error and str(validation_error).startswith("输出过碎："):
            prompt = (
                "下面是一次记忆提取的原始回答。事实被拆得太碎，请只基于原回答已有信息压缩整理；"
                "不得新增事实，不得改变人物归属、否认、纠正或约定结果。\n"
                "要求：\n"
                '- 顶层只能包含数组 "memories"；最多 5 条 memory。\n'
                '- 每条 memory 只包含 key_facts，每条最多 5 个 fact；整个窗口总 fact 最多 5 条。\n'
                "- 合并同一事件、同一段关系变化或同一结论的过程话语，保留原因、发展和最终结果；"
                "不要逐句摘录。\n"
                "- 必须优先保留明确事实、重要冲突、关系变化、承诺、约定、边界、偏好、"
                "后续纠正和最终结论；可删除仅用于铺垫的动作或重复说法。\n"
                "- 合并后的 importance 取被合并事实中的最高值；topics 去重。\n"
                "- 每条 key_fact 必须包含 fact、topics、importance；persona_reaction 可选。\n"
                "只输出 JSON，不要解释。\n\n"
                f"原始回答：\n{response_text}"
            )
            system_prompt = "你只负责把过碎的记忆事实压缩为少量完整事实，不补造新信息。"
            return await self._call_llm_with_retry(prompt, system_prompt)

        prompt = (
            "下面是一次记忆提取的原始回答。只把它整理成合法 JSON；"
            "不得新增、删除、合并、拆分或改写任何 fact，也不得改变 importance。\n"
            "要求：\n"
            '- 顶层只能包含数组 "memories"；每条 memory 只包含 key_facts。\n'
            '- 每条 key_fact 必须包含 fact、topics、importance；time 和 persona_reaction 可选。\n'
            "- 删除其他字段；不得补造缺失内容。\n"
            "如果原回答缺少某个事实判断所需的信息，不要猜测或补造；保留缺失，"
            "让后续校验拒绝。只输出 JSON，不要解释。\n\n"
            f"原始回答：\n{response_text}"
        )
        system_prompt = "你只负责修复 JSON 表达形式，不负责重新总结或判断记忆价值。"
        return await self._call_llm_with_retry(prompt, system_prompt)

    async def process_conversation_result(
        self,
        messages: list[Message],
        is_group_chat: bool = False,
        persona_id: str | None = None,
        topic_candidates: list[dict[str, Any] | str] | None = None,
        source_scope: str | None = None,
    ) -> MemoryProcessingResult:
        """
        处理对话历史，返回 store / skip / invalid 三态结果。

        Args:
            messages: 消息列表(Message对象)
            is_group_chat: 是否为群聊
            persona_id: 人格ID,用于获取人格提示词

        格式不合格时只请求一次格式修复；修复仍失败返回 invalid。
        Provider 或其他运行错误仍向上抛出，由调用方沿用既有重试机制。
        """
        if not messages:
            raise ValueError("消息列表不能为空")

        # 1. 格式化对话历史
        conversation_text = self._format_conversation(messages)

        # 2. 选择合适的提示词模板（每次从 PromptManager 读取，确保 WebUI 保存后立即生效）
        # 使用 replace 而非 format，避免对话内容中的大括号导致解析错误
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt = self._get_chat_prompt(is_group_chat).replace(
            "{conversation}", conversation_text
        ).replace("{current_date}", current_date)
        candidates_payload = json.dumps(
            topic_candidates or [], ensure_ascii=False, separators=(",", ":")
        )
        prompt += (
            "\n\n## 当前 scope 可复用的 topic 候选\n"
            f"{candidates_payload}\n"
            "只有确认是同一概念时才复用候选名称；不要做近义词合并。"
        )
        prompt = f"{prompt}\n\n{self._build_admission_output_contract(is_group_chat)}"

        # 3. 调用LLM生成结构化记忆
        conversation_type = "群聊" if is_group_chat else "私聊"
        try:
            logger.info(
                f"[MemoryProcessor] 准备调用 LLM，对话类型={conversation_type}, 消息数={len(messages)}"
            )
            logger.debug(f"[MemoryProcessor] Prompt 模板长度={len(prompt)}")
            logger.debug(
                f"[MemoryProcessor] 发送给LLM的对话内容（前500字符）:\n{conversation_text[:500]}"
            )

            # 构建 system_prompt，嵌入人格提示
            system_prompt = await self._build_system_prompt_with_persona(persona_id)
            logger.debug(f"[MemoryProcessor] System Prompt: {system_prompt[:200]}...")

            llm_response_text = await self._call_llm_with_retry(
                prompt=prompt,
                system_prompt=system_prompt,
            )

            logger.info(
                f"[MemoryProcessor]  LLM 响应成功，响应长度={len(llm_response_text)}"
            )
            logger.debug(f"[MemoryProcessor] LLM 原始响应内容:\n{llm_response_text}")

            # 4. 原始回答先过严格格式门；失败时只请求一次格式修复。
            try:
                structured_data = self._parse_llm_response(
                    llm_response_text, is_group_chat
                )
            except InvalidMemoryOutputError as first_error:
                logger.warning(
                    f"[MemoryProcessor] 原始回答格式不合格，尝试一次格式修复: {first_error}"
                )
                repaired_text = await self._repair_llm_response_format(
                    llm_response_text, is_group_chat, first_error
                )
                try:
                    structured_data = self._parse_llm_response(
                        repaired_text, is_group_chat
                    )
                except InvalidMemoryOutputError as second_error:
                    logger.warning(
                        f"[MemoryProcessor] 格式修复后仍不合格: {second_error}"
                    )
                    return MemoryProcessingResult(
                        status="invalid",
                        error=str(second_error),
                    )

            # 4.5 逐 fact 准入，并保留每个单中心 memory unit。
            try:
                admitted_units, stored_count, skipped_count = (
                    self._prepare_admitted_units(structured_data)
                )
            except InvalidMemoryOutputError as quality_error:
                logger.warning(f"[MemoryProcessor] 候选事实不合格: {quality_error}")
                return MemoryProcessingResult(
                    status="invalid",
                    error=str(quality_error),
                )

            if not admitted_units:
                logger.info(
                    f"[MemoryProcessor] 本窗口没有获准保存的事实，跳过 {skipped_count} 条候选"
                )
                return MemoryProcessingResult(
                    status="skip",
                    stored_fact_count=0,
                    skipped_fact_count=skipped_count,
                )

            try:
                records = self._build_v3_storage_records(
                    admitted_units=admitted_units,
                    messages=messages,
                    is_group_chat=is_group_chat,
                    topic_candidates=topic_candidates,
                    source_scope=source_scope,
                )
            except InvalidMemoryOutputError as quality_error:
                logger.warning(f"[MemoryProcessor] 事实来源或时间不合格: {quality_error}")
                return MemoryProcessingResult(
                    status="invalid",
                    error=str(quality_error),
                )

            for record in records:
                record.content = self._apply_source_time_tags(
                    record.content, record.metadata, messages
                )
            first = records[0]

            logger.info(
                f"[MemoryProcessor] 成功生成 {len(records)} 条单中心记忆，"
                f"获准事实={stored_count}, 类型={conversation_type}"
            )
            logger.debug(
                f"[MemoryProcessor] 首条记忆内容（前200字符）:\n{first.content[:200]}"
            )

            return MemoryProcessingResult(
                status="store",
                content=first.content,
                metadata=first.metadata,
                importance=first.importance,
                stored_fact_count=stored_count,
                skipped_fact_count=skipped_count,
                records=records,
            )

        except Exception as e:
            logger.error(f"[MemoryProcessor] 处理对话历史失败: {e}", exc_info=True)
            # 不再降级处理，直接向上抛出异常，由调用方处理重试逻辑
            raise

    async def process_conversation(
        self,
        messages: list[Message],
        is_group_chat: bool = False,
        persona_id: str | None = None,
    ) -> tuple[str, dict[str, Any], float]:
        """Compatibility tuple API for callers that require a stored memory."""
        result = await self.process_conversation_result(
            messages=messages,
            is_group_chat=is_group_chat,
            persona_id=persona_id,
        )
        if result.status == "skip":
            raise MemoryAdmissionSkipped("本窗口没有之后需要记住或接续的事实")
        if result.status == "invalid":
            raise InvalidMemoryOutputError(result.error or "记忆总结结果不合格")
        records = result.iter_records()
        if len(records) != 1:
            raise InvalidMemoryOutputError(
                "该兼容接口只能返回一条记忆；请改用 process_conversation_result"
            )
        return result.content, result.metadata, result.importance

    def _apply_source_time_tags(
        self,
        content: str,
        metadata: dict[str, Any],
        messages: list[Message],
    ) -> str:
        """Attach source dates without asking the LLM to infer them."""
        if not self.config.get("include_source_time_tags", True) or not messages:
            return content

        timestamps = sorted(float(message.timestamp) for message in messages)
        start = datetime.fromtimestamp(timestamps[0]).astimezone()
        end = datetime.fromtimestamp(timestamps[-1]).astimezone()
        dates = sorted(
            {
                datetime.fromtimestamp(value).strftime("%Y-%m-%d")
                for value in timestamps
            }
        )
        label = dates[0] if len(dates) == 1 else f"{dates[0]} - {dates[-1]}"

        metadata["time_tags"] = dates
        metadata["source_time_start"] = start.isoformat()
        metadata["source_time_end"] = end.isoformat()
        metadata["source_time_label"] = label
        return content

    def _format_conversation(self, messages: list[Message]) -> str:
        """
        格式化对话历史为文本

        Args:
            messages: 消息列表(Message对象)

        Returns:
            格式化后的对话文本
        """

        formatted_lines = []
        for i, msg in enumerate(messages, 1):
            logger.debug(
                f"[_format_conversation] 消息#{i}: "
                f"sender_id={msg.sender_id}, sender_name={msg.sender_name}, "
                f"role={msg.role}, group_id={msg.group_id}"
            )

            content_text = self._message_content_to_text(msg.content)
            sender_info = self._format_sender_info(msg)
            formatted_line = f"[M{i}] {sender_info} {content_text}".rstrip()
            formatted_lines.append(formatted_line)
            if msg.group_id:
                logger.debug(
                    f"[_format_conversation] 消息#{i} 格式化结果(群聊): {formatted_line[:100]}..."
                )
            else:
                logger.debug(
                    f"[_format_conversation] 消息#{i} 格式化结果(私聊): {sender_info[:50]}..."
                )
        return "\n".join(formatted_lines)

    @staticmethod
    def _extract_participant_identities(
        messages: list[Message],
    ) -> list[dict[str, Any]]:
        """Build stable graph identities from message sender IDs, not LLM names."""
        identities: dict[str, dict[str, Any]] = {}
        for message in messages:
            if message.role == "system":
                continue
            sender_id = str(message.sender_id or "").strip()
            if not sender_id:
                continue
            platform = str(message.platform or "unknown").strip().lower() or "unknown"
            identity_key = f"{platform}:{sender_id}"
            display_name = str(message.sender_name or sender_id).strip() or sender_id
            is_bot = bool(
                message.metadata.get("is_bot_message", False)
                or message.role == "assistant"
            )

            identity = identities.setdefault(
                identity_key,
                {
                    "identity_key": identity_key,
                    "sender_id": sender_id,
                    "platform": platform,
                    "display_name": display_name,
                    "aliases": [],
                    "is_bot": is_bot,
                },
            )
            identity["display_name"] = display_name
            identity["is_bot"] = bool(identity["is_bot"] or is_bot)
            if display_name not in identity["aliases"]:
                identity["aliases"].append(display_name)

        return list(identities.values())

    @staticmethod
    def _format_sender_info(msg: Message) -> str:
        time_str = datetime.fromtimestamp(msg.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        display_name = msg.sender_name if msg.sender_name else msg.sender_id or "未知"
        is_bot = msg.metadata.get("is_bot_message", False) or msg.role == "assistant"
        if is_bot:
            return f"[Bot: {display_name} | ID: {msg.sender_id} | {time_str}]"
        return f"[{display_name} | ID: {msg.sender_id} | {time_str}]"

    @classmethod
    def _message_content_to_text(cls, content: Any) -> str:
        return Message.content_to_text(content)

    @classmethod
    def _message_part_to_text(cls, part: Any) -> tuple[str, bool]:
        return Message._content_part_to_text(part)
