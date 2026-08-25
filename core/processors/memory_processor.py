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

from ..memory_scope import resolve_persona_display_name
from ..models.conversation_models import Message
from ..models.memory_contract import (
    build_participant_identity,
    normalize_concept_name,
)
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
        """Return the non-overridable memory-output contract and examples."""
        peer = "群成员" if is_group_chat else "对方"
        return (
            "## 输出格式\n"
            '- 只输出合法 JSON，结构必须是 {"memories":[{"key_facts":[...]}]}。\n'
            '- 每个 fact 必须包含 "fact"、"topics"、"importance"；符合下方条件时增加 '
            '"persona_reaction"；不要输出其他字段。\n'
            "\n"
            "## 完整示例\n"
            'topic 候选：["项目进度","工作安排"]\n'
            "对话：\n"
            "[M1] [小林 | ID: 10001 | 2025-11-19 18:42:10] "
            "我原本说周五交项目，刚确认改成下周一了。\n"
            "[M2] [Bot: 助手 | 2025-11-19 18:42:25] "
            "好，我记住最终是下周一。第一次负责这么大的项目，会紧张吗？\n"
            "[M3] [小林 | ID: 10001 | 2025-11-19 18:43:02] "
            "有一点，不过交付时间就是下周一，别再记成周五。\n"
            "\n"
            "对应输出：\n"
            "{\n"
            '  "memories": [\n'
            "    {\n"
            '      "key_facts": [\n'
            "        {\n"
            '          "fact": "2025-11-19傍晚，小林确认项目最终于2025-11-24交付，并表示对此有些紧张。",\n'
            '          "topics": ["项目进度"],\n'
            '          "importance": 0.8,\n'
            '          "persona_reaction": {\n'
            '            "emotion": "关心",\n'
            '            "thought": "我想记得这件事对小林很重要"\n'
            "          }\n"
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "\n"
            "示例要点：丢弃已被纠正的“周五”，将“下周一”换算为 2025-11-24；"
            "按消息时间写“2025-11-19傍晚”，复用候选“项目进度”，并把同一事件合成一个 fact。"
            "Bot 的复述不是新事实；助手的明确关心作为人格反应保留。\n"
            "\n"
            "persona_reaction 与单个 fact 配对，只包含 emotion 和 thought。若当前人格对关系变化、"
            "明确情绪、冲突修复、重要经历、承诺或边界形成了真实且可长期保留的反应，优先写；"
            "纯客观事实、复述 fact 或勉强揣测则省略。thought 使用当前 Bot 的第一人称，保持简短，不补造事实。\n"
            "\n"
            "纯技术事实可以省略 persona_reaction，例如：\n"
            "{\n"
            '  "fact": "2025-11-19傍晚，小林确认项目使用 Python 3.12。",\n'
            '  "topics": ["项目技术"],\n'
            '  "importance": 0.5\n'
            "}\n"
            "\n"
            "## 提取规则\n"
            "- 整个窗口最多输出 5 个 fact，每条 memory 也不得超过 5 个。\n"
            "- 只输出值得长期接续的事实；寒暄、填充、临时报错、无后果的即时状态和重复内容直接不输出。\n"
            "- 读完整窗口；同一件事只保留结尾已确认的状态或最终约定，不保存被后续否认、纠正的版本。\n"
            f"- 只提取本窗口新确认的信息；Bot 复述的旧记忆、人格设定、单方面推测及未经{peer}确认的建议或旧约定不写入。\n"
            "- 承诺或双方约定写清谁提出、是否接受；边界和偏好写清属于谁。短期定时任务不由记忆系统代办。\n"
            "- 单次玩笑、昵称或亲昵称呼不自动成为稳定偏好；单次重要冲突、修复或共同意义仍可保存。\n"
            "- 一条 memory 只围绕一个中心；同一事件、关系变化或结论的原因、发展和结果合成一个 fact，不逐句拆分。topics 只属于该 fact。\n"
            f"- fact 必须中性、自包含，并使用{peer}的具体昵称。描述当前 Bot 自己时只用第一人称“我”；[Bot: ...] 不是用户。\n"
            "\n"
            "## 时间写法\n"
            "- 本窗口事实按承载它的消息时间写“明确日期＋自然时段”（如“2026-08-24晚”）；"
            "相对时间也按该消息改写为具体日期和自然时段。时段可自然发挥。若事实明确指向其他时刻，则保留事件时间。不要输出 time 字段。\n"
            "\n"
            "## importance 评分\n"
            "先判断事实是否值得保存，再按该 fact 对未来交流的参考价值评分：\n"
            "- 0.9-1.0：关键需求、重大决定、承诺或边界、强烈情绪表达、重要关系变化。\n"
            "- 0.7-0.8：明确计划、偏好、具体要求、重要个人信息。\n"
            "- 0.5-0.6：有帮助但影响有限的日常事实。\n"
            "- 0.3-0.4：次要、未来参考价值较低的事实。\n"
            "- 0.0-0.2：仅有极弱参考价值的边缘事实；纯测试或没有实质内容不应输出。\n"
            "\n"
            '没有值得保存的事实时，输出 {"memories":[]}。'
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
        return (
            "你负责从对话窗口提取可长期接续的事实，并严格输出指定 JSON。"
            "根据每条消息的发送时间，在事实正文中使用具体日期和自然时段；"
            f"不单独输出时间字段。当前日期时间：{current_date}"
        )

    @staticmethod
    def _build_enhanced_prompt_fallback(
        base_prompt: str, persona_prompt: str, current_date: str
    ) -> str:
        """后备增强 system prompt（当 PromptManager 不可用时）"""
        return (
            f"{base_prompt}\n\n"
            f"## 当前人格\n"
            f"{persona_prompt}\n\n"
            "人格用于理解 Bot 的身份，并优先为能体现当前人格态度、感受或关系意义的 fact "
            "生成配套短句 persona_reaction；纯客观、只有复述或需要勉强揣测时省略。"
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
                completion_text = response.completion_text
                if completion_text is None:
                    completion_text = ""
                if not isinstance(completion_text, str):
                    completion_text = str(completion_text)
                if len(completion_text) == 0:
                    raise RuntimeError("LLM 返回空响应（0 字符）")
                return completion_text
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
                "- 每条 key_fact 必须包含 fact、topics、importance；字段 persona_reaction 本身仍可选。\n"
                "- 已有 persona_reaction 必须跟随对应事实；合并时可把已有反应收束为一个，"
                "不得凭空新增或丢失。\n"
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
            '- 每条 key_fact 必须包含 fact、topics、importance；字段 persona_reaction 本身仍可选。\n'
            "- 已有 persona_reaction 必须原样保留，不得补造新的反应。\n"
            "- 删除其他字段；不得补造缺失内容。\n"
            "如果原回答缺少某个事实判断所需的信息，不要猜测或补造；保留缺失，"
            "让后续校验拒绝。只输出 JSON，不要解释。\n\n"
            f"原始回答：\n{response_text}"
        )
        system_prompt = "你只负责修复 JSON 表达形式，不负责重新总结或判断记忆价值。"
        return await self._call_llm_with_retry(prompt, system_prompt)

    async def _parse_response_with_single_repair(
        self, response_text: str, is_group_chat: bool
    ) -> dict[str, Any]:
        """Parse one extraction response, allowing one structure-only repair."""
        try:
            return self._parse_llm_response(response_text, is_group_chat)
        except InvalidMemoryOutputError as first_error:
            logger.warning(
                f"[MemoryProcessor] 原始回答格式不合格，尝试一次格式修复: {first_error}"
            )
            repaired_text = await self._repair_llm_response_format(
                response_text, is_group_chat, first_error
            )
            try:
                return self._parse_llm_response(repaired_text, is_group_chat)
            except InvalidMemoryOutputError as second_error:
                logger.warning(
                    f"[MemoryProcessor] 格式修复后仍不合格: {second_error}"
                )
                raise second_error from first_error

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
        conversation_text = self._format_conversation(messages, persona_id=persona_id)

        # 2. 选择合适的提示词模板（每次从 PromptManager 读取，确保 WebUI 保存后立即生效）
        # 使用 replace 而非 format，避免对话内容中的大括号导致解析错误
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt = self._get_chat_prompt(is_group_chat).replace(
            "{conversation}", conversation_text
        ).replace("{current_date}", current_date)
        # 只把候选名字给 LLM：topic_id 是系统内部稳定标识，
        # 暴露给模型只会诱导它抄错字段（历史上出现过把 topic_id
        # 当名字输出的脏数据）。名字 -> topic_id 的映射由系统内部完成。
        visible_names: list[str] = []
        for candidate in topic_candidates or []:
            if isinstance(candidate, dict):
                name = str(
                    candidate.get("name") or candidate.get("final_name") or ""
                ).strip()
            else:
                name = str(candidate).strip()
            if name:
                visible_names.append(name)
        candidates_payload = json.dumps(
            visible_names, ensure_ascii=False, separators=(",", ":")
        )
        prompt += (
            "\n\n## 当前 scope 可复用的 topic 候选\n"
            f"{candidates_payload}\n"
            "同一概念原样复用候选名称；没有合适候选就写一个简短明确的新 topic，系统会创建它；"
            "不要强行套用近义候选。"
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
                structured_data = await self._parse_response_with_single_repair(
                    llm_response_text, is_group_chat
                )
            except InvalidMemoryOutputError as parse_error:
                return MemoryProcessingResult(
                    status="invalid",
                    error=str(parse_error),
                )

            # 4.5 将已校验的 fact 组装为单中心 memory unit。
            try:
                admitted_units, stored_count, skipped_count = (
                    self._prepare_storage_units(structured_data)
                )
            except InvalidMemoryOutputError as quality_error:
                logger.warning(f"[MemoryProcessor] 候选事实不合格: {quality_error}")
                return MemoryProcessingResult(
                    status="invalid",
                    error=str(quality_error),
                )

            if not admitted_units:
                logger.info(
                    "[MemoryProcessor] LLM 明确返回空候选，本窗口正常跳过"
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
                    persona_id=persona_id,
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

    def _format_conversation(
        self,
        messages: list[Message],
        persona_id: str | None = None,
    ) -> str:
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
            sender_info = self._format_sender_info(
                msg,
                persona_id=persona_id,
                persona_display_aliases=self.config.get(
                    "persona_display_aliases", ""
                ),
            )
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
        persona_id: str | None = None,
        persona_display_aliases: Any = "",
    ) -> list[dict[str, Any]]:
        """Build stable user accounts and persona-scoped Bot identities."""
        identities: dict[str, dict[str, Any]] = {}
        for message in messages:
            if message.role == "system":
                continue
            sender_id = str(message.sender_id or "").strip()
            if not sender_id:
                continue
            platform = str(message.platform or "unknown").strip().lower() or "unknown"
            display_name = str(message.sender_name or sender_id).strip() or sender_id
            is_bot = bool(
                message.metadata.get("is_bot_message", False)
                or message.role == "assistant"
            )
            account_identity = build_participant_identity(
                platform=platform,
                sender_id=sender_id,
                display_name=display_name,
                is_bot=is_bot,
            )
            candidate = account_identity
            resolved_persona_id = str(persona_id or "").strip()
            if is_bot and resolved_persona_id:
                persona_key = normalize_concept_name(resolved_persona_id).casefold()
                persona_display = resolve_persona_display_name(
                    resolved_persona_id,
                    persona_display_aliases,
                    sender_name=display_name,
                    sender_id=sender_id,
                )
                persona_aliases = [persona_display]
                if resolved_persona_id not in persona_aliases:
                    persona_aliases.append(resolved_persona_id)
                if (
                    display_name != sender_id
                    and not display_name.isdigit()
                    and display_name not in persona_aliases
                ):
                    persona_aliases.append(display_name)
                candidate = {
                    "identity_kind": "persona",
                    "identity_key": f"persona:{persona_key}",
                    "persona_id": resolved_persona_id,
                    "sender_id": sender_id,
                    "platform": platform,
                    "display_name": persona_display,
                    "aliases": persona_aliases,
                    "account_identity_keys": [account_identity["identity_key"]],
                    "sender_ids": [sender_id],
                    "platforms": [platform],
                    "is_bot": True,
                }
            identity_key = str(candidate["identity_key"])

            identity = identities.setdefault(identity_key, candidate)
            identity["display_name"] = str(candidate["display_name"])
            identity["is_bot"] = bool(identity["is_bot"] or is_bot)
            for alias in candidate.get("aliases", []):
                if alias not in identity["aliases"]:
                    identity["aliases"].append(alias)
            for key in ("account_identity_keys", "sender_ids", "platforms"):
                if key not in candidate:
                    continue
                values = identity.setdefault(key, [])
                for value in candidate[key]:
                    if value not in values:
                        values.append(value)

        return list(identities.values())

    @staticmethod
    def _format_sender_info(
        msg: Message,
        persona_id: str | None = None,
        persona_display_aliases: Any = "",
    ) -> str:
        time_str = datetime.fromtimestamp(msg.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        display_name = msg.sender_name if msg.sender_name else msg.sender_id or "未知"
        is_bot = msg.metadata.get("is_bot_message", False) or msg.role == "assistant"
        if is_bot:
            display_name = resolve_persona_display_name(
                persona_id,
                persona_display_aliases,
                sender_name=display_name,
                sender_id=msg.sender_id,
            )
            return f"[Bot: {display_name} | {time_str}]"
        return f"[{display_name} | ID: {msg.sender_id} | {time_str}]"

    @classmethod
    def _message_content_to_text(cls, content: Any) -> str:
        return Message.content_to_text(content)

    @classmethod
    def _message_part_to_text(cls, part: Any) -> tuple[str, bool]:
        return Message._content_part_to_text(part)
