---
stage: S_TODO
status: Done
depends_on: []
updated: 2026-08-24
document_role: cleanup-backlog
---

# S_TODO：遗留清理清单

本页记录已确认的遗留清理及执行结果。`Sfuture.md` 只记录尚未立项的新增能力；本页不新增功能。

## 状态总览

| 项目 | 状态 | 结果 |
|---|---|---|
| T1 recent_memory_* 死链 | Done | 配置、实现、缓存键、文档和测试已删除 |
| T2 memory_type_filter 孤儿配置 | Done | 配置与 event_only 死分支已删除，保留真实检索策略过滤 |
| T3 图谱缺 Bot 节点 | Done | 纠正预调查结论，按上游正式链路恢复稳定 Bot 节点 |
| T4 工具自创 topic | Done | 主动工具先按正文召回候选 topic，选定后才写入 |
| T5 单条事实默认预算 300 | Done | schema、校验、生产 fallback、文档和评测脚本已统一为 300 |
| T6 WebUI 时间格式 | Done | 展示层统一兼容时间戳与旧微秒字符串 |
| T7 LLM 0 字符响应重试 | Done | Provider 最终交给插件的正文为 0 字符时走原调用重试 |
| T8 工具人物身份不复用 | Done | 主动工具复用当前事件的稳定 sender 身份 |

## 已完成

### T1：删除 recent_memory_* 老链路

已删除 `recent_memory_count`、`recent_memory_max_age_hours` 的 schema、校验和初始化透传，移除 legacy recent 查询/合并方法、调用点及缓存键参数，并清理中英文设置说明、插件翻译和相关测试。当前短期连续性只由独立的 `recent_block_*` 链路承担，不恢复绕过相关性排名的固定槽位。

### T2：删除 memory_type_filter 孤儿配置

已删除 `memory_type_filter` 的 schema、校验、初始化透传、缓存键和 `event_only` 分支，并清理插件翻译和测试。`_filter_by_retrieval_policy` 仍保留 active 状态、重要性和相似度过滤；`atom_types` 的存量元数据展示不在本项范围。

### T3：恢复 Bot 图节点

此前“上游也过滤 Bot”的预调查结论有误。上游从提交 `50a732d` 起的正式生产链路是：`MemoryProcessor` 把包括 assistant/Bot 在内的发送者写入 `participant_identities`，`GraphExtractor._participant_nodes()` 再把这些稳定身份建成 person 节点；因此原版图谱能看见 Bot 节点。

本 fork 的 v3 canonical 图路径只消费 fact 级 `participant_refs`，而写侧又跳过 `is_bot`，所以 Bot 节点在 S1/S3 改造中丢失。现已按上游身份链恢复所有稳定发送者节点（包括 Bot），并保持 S3 的约束：不重建旧版 person×fact 全组合边。只有 fact 明确提到 Bot 名称或按不可覆盖契约使用第一人称“我”时，才建立该 fact 的 `mentioned_in` 边。

### T5：单条事实默认注入预算改为 300

默认值已从 260 统一改为 300，覆盖 schema、Pydantic 校验、自动召回、Agent 搜索 fallback、canonical packer、初始化配置、说明文档和离线评测脚本。已有显式配置继续优先，不强行覆盖用户自定义值。

### T6：统一 WebUI 时间显示

前端新增统一时间格式化：兼容秒/毫秒时间戳、ISO 字符串、日期字符串和旧 SQLite/Python 微秒字符串。列表、详情、速览和召回详情统一显示到秒；无时区的旧墙钟时间只去掉微秒，不做错误的 UTC 偏移。

### T7：LLM 0 字符响应重试

`provider.text_chat()` 成功返回、但最终交给插件的 `completion_text` 长度为 0 时，按调用失败进入原有最多 3 次指数退避重试；无论上游是直接空回，还是生成内容后被服务商审核拦截而留下 0 字符，插件侧都按同一种空响应处理。重试使用同一 prompt，不把空正文送去 JSON 修复。`{"memories":[]}` 是模型明确写出的合法空候选，仍按原逻辑直接 `skip`；非空回答沿用原有解析、格式修复和事实过滤流程。召回预算装配为空不是 LLM 响应，不进入这条重试链。

### 补充：删除事实正文硬编码拦截

已删除对“某用户”“某人”“后来”“这件事”等固定词语的包含或前缀匹配。此类字符串判断无法可靠识别事实是否自包含，会误杀“后来张三决定……”等已有明确主体的句子。

同时删除未经实际问题验证的 `importance <= 0.2` 写库阈值和旧 `action: skip` 兼容分支。importance 只作为事实元数据，不再决定候选是否写入；当前格式若出现 `action` 会按多余字段拒绝并进入原有格式修复。事实正文仍需通过非空、字段类型、数量和结构校验；没有事实时唯一的正常信号是 `{"memories":[]}`。

## 工具链

### T4：Bot 调主动记忆工具时自创 topic

主动记忆工具改为两阶段调用。第一次只传待记正文，不写库；工具在相同 memory scope 和 persona 内，用该正文走现有 canonical fact 的 FTS 与向量候选路由，再从相关事实的 `topic_refs` 聚合、去重并返回最多 5 个稳定 `topic_id / name`。第二次必须明确传回候选 `topic_id`、单独声明 `new_topic`，或确认无 topic 写入，才正式保存。

本项只给主动记忆工具增加小规模的 query-aware topic 选择；自动 LLM 总结仍使用原有较大候选池，本轮不调整其候选生成规模和策略。

### T8：主动记忆工具与自动总结的人物身份不复用

主动记忆工具现在从真实事件读取 platform、sender ID、sender name 和可用的 Bot self ID，按自动总结相同的 `平台:sender_id` 规则生成 `participant_identities`。工具参数中的参与者名字若唯一命中当前发送者或 Bot 的名称、ID、别名，就写入同一稳定 `participant_id`；没有命中真实事件身份的其他被提及人物仍保留名字派生节点，同一名字命中多个身份时不猜测、不建立事实人物绑定。自动总结沿用同一歧义规则。

## 状态词

```text
Deferred（已确认、按范围暂不执行） | In Progress（动手了） | Done（清理完成）
```
