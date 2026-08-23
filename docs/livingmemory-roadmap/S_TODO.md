---
stage: S_TODO
status: Deferred
depends_on: []
updated: 2026-08-24
document_role: cleanup-backlog
---

# S_TODO：未来清理清单

已确认需要清理、但暂不执行的技术债。每条都先记现状、为什么暂留、清理方案和风险，等实际动手时按本文件执行，不让它再靠记忆或口头约定。

与 `Sfuture.md` 的边界：`Sfuture` 记录尚未立项的**新增能力**愿景；本文件只记录已确认的**遗留清理**，不新增功能。

## 清理项

### T1：recent_memory_* 老链路死配置与死代码

**现状**

- `recent_memory_count`（「近期记忆固定槽位」）：S5 已废弃，schema 默认 0、validator 默认 0；实现 `_merge_recent_memories` / `_get_recent_memory_results` 仍在代码里（`memory_engine_write_ops.py`），调用点还在 `memory_engine_crud.py`，但 `recent_count <= 0` 短路，生产不生效。
- `recent_memory_max_age_hours`（「近期记忆时间窗口」，默认 72）：唯一消费方是 `_get_recent_memory_results` 的时间过滤，随固定槽位一起短路，是死默认值。
- 两值还进入检索缓存键（`memory_engine_write_ops.py` 缓存键）。
- 配置页仍在显示两项：`_conf_schema.json` 的「近期记忆固定槽位（已废弃）」和「近期记忆时间窗口（小时）」。

**为什么暂留**

- 不删不改变任何生产行为（默认配置下整条链路不执行）；删除会连带清理 4 个测试文件的用例和缓存键，为页面少一行冒回归风险不划算。

**清理方案**

1. `_conf_schema.json`：删两条配置项。
2. `core/base/config_validator.py`：删 `recent_memory_count`、`recent_memory_max_age_hours` 两个 Field。
3. `core/plugin_initializer_finalize.py`：删两行透传。
4. `core/managers/memory_engine_write_ops.py`：删 `_get_recent_memory_results`、`_merge_recent_memories` 方法 + 缓存键两参数。
5. `core/managers/memory_engine_crud.py`：删 `_merge_recent_memories` 调用点。
6. `docs/configuration.md`、`docs/en/configuration.md`：删对应表格行。
7. 测试：`tests/test_memory_engine.py`（742/784/808）、`tests/test_event_handler.py`（242）、`tests/integration/test_real_db_end_to_end.py`（380）、`tests/test_config_manager.py`（21/91/92/102）清理相关用例。

**风险**

低。改动面全是死路径，删后跑全量测试确认无引用残留即可。

**关联**：`S5.md` / `README.md` 问题表 I10 已记录"已取消 recent 固定槽位"；本项是代码与配置的收尾清理，不是行为变更。

### T2：`memory_type_filter` 记忆类型过滤（上游原版就无消费者的孤儿配置）

**现状**

- `memory_type_filter`（「记忆类型过滤」，`all` / `event_only`）：**上游原版就无生产消费者**。上游 Atom 体系只写不读——`atom_store` / `atom_retriever` 虽已初始化，但 `atom_retriever` 从未进入生产召回（`search_memories` 只走 dual_route / hybrid retriever）；`atom_types` 元数据会写入文档，`event_only` 过滤（`_filter_by_retrieval_policy` 的 atom_types 分支）从无生产验证，属于孤儿功能。
- fork 的 canonical fact 生产路径（`fact_retriever`）也不消费它；仅 legacy 兼容路径（`fact_retriever is None`）会经过 `_filter_by_retrieval_policy`。
- 引用点：`_conf_schema.json` 配置条目、`config_validator.py:75`、`plugin_initializer_finalize.py:123`、`memory_engine_write_ops.py:170`（缓存键）与 `182-214`（event_only 分支）、`memory_engine_crud.py:749`（legacy 调用点）。

**为什么暂留**

- 默认 `all` 不影响任何行为；`event_only` 只在 legacy 路径可见且无人使用。删除属于收尾清理，不改变生产。

**清理方案**

1. `_conf_schema.json`：删 `memory_type_filter` 条目。
2. `core/base/config_validator.py`：删 Field。
3. `core/plugin_initializer_finalize.py`：删透传。
4. `core/managers/memory_engine_write_ops.py`：删 `_filter_by_retrieval_policy` 内 event_only 分支 + 缓存键参数。注意：该函数本身在 legacy 路径仍承担重要性/相似度过滤，**只删 event_only 分支，不删整个函数**。
5. 测试：`tests/test_memory_engine.py`（event_only 用例）、`tests/test_config_manager.py`、`tests/test_plugin_i18n_coverage.py` 相应清理。
6. `atom_types` 元数据写入（`memory_processor_build.py:512`、`memory_engine_crud.py:296`）暂不评估，图谱/面板可能仍展示。

**风险**

低。生产 canonical 路径不消费；legacy 路径删 event_only 分支不影响重要性/相似度过滤。

**关联**：S4 停用独立 Atom 的收尾；与 T1（recent 老链路）同批清理可合并提交。

### T3：图谱人物缺 "bot"（预调查：设计过滤，非丢失）

**现状**

- 写侧 `core/processors/memory_processor_build.py:200-202`：`_participant_refs_for_fact` 里显式 `if bool(identity.get("is_bot")): continue`——fact 的参与人绑定**跳过 bot**。
- canonical 图谱路径（`_extract_from_canonical`）只从 fact 的 `participant_refs` 建边，fact 里没 bot → 图谱无 bot 节点、无相关连线。
- `graph_extractor._participant_nodes`（55-102）本身不过滤 is_bot，但 canonical 路径走不到它建 bot。
- **图谱人物怎么来的**：消息 sender_id → `_extract_participant_identities`（`memory_processor.py:668-704`，身份键=`平台:sender_id`，含显示名/别名/is_bot）→ fact 的 `participant_refs`（LLM 事实文本与别名词面匹配，只绑非 bot）→ 图节点 `account:<identity_key>`。
- **上游对比**：上游原版同样在消息侧跳过 bot（`memory_processor.py:570-571`），bot 不入图谱是**上游就有的设计**，fork 没丢东西。

**待决**：是否要让 bot（Bot 自己）入图谱？这是设计取舍，需用户拍板。若要做：写侧去掉 `is_bot` 过滤 + bot 的 sender_id 身份记录需稳定（下游方向）。

**已核实（2026-08-24）**：`_participant_refs_for_fact`（`memory_processor_build.py:196-217`）显式 `if bool(identity.get("is_bot")): continue`；上游原版同样跳过 bot。结论维持：非丢失、是上游设计，状态仍待用户拍板。

### T4：bot 调记忆工具时自创 topic，图谱被搅乱

**现状**

- agent 调记忆工具（recall/memorize）写入时，LLM 不知道现有 topic 词表，会自创 4~5 个新 topic，图谱出现一次性垃圾 topic 节点。
- 与 `Sfuture.md` 愿景六（topic 候选生成：全量候选→检索 top-K）同源，但愿景六针对**规模化后的候选列表**，工具侧自创是**眼前实害**，不等愿景六。

**已查明（2026-08-24 核实）**

- 自动总结路径**已落地事前约束**：`memory_processor.py:446-466` 把现有 topic 候选名单注入 LLM prompt（「只有确认是同一概念时才复用候选名称；不要做近义词合并」），配合 `_resolve_topic`（`memory_processor_build.py:134-177`）按候选池复用 topic_id、拒绝机器 ID。
- 工具路径（`memory_memorize_tool.py`）确认**不经过 `_resolve_topic`**：它调 `get_topic_candidates` 拿候选池传给 `build_explicit_memory_record`（`memory_processor_build.py:325-418`），该函数有 sanitize（机器 ID 退化为候选池人话名或丢弃）+ topic_catalog 按名复用（decision=reused），但 **LLM 起名时看不到候选名单**——约束是「事后对齐」不是「事前约束」，自创新名仍会新建 topic 节点。

**下一步**

- 工具侧补事前约束：把候选 topic 名单注入记忆工具的 schema 描述或调用上下文，LLM 优先复用；或给工具加候选校验参数。

### T5：单条事实注入上限改为 300

**状态：待执行（用户未授权）**

- 2026-08-24 曾**未授权**将默认值改为 300（`_conf_schema.json` + `config_validator.py` 均改、实机同步并重载）；用户要求回退，**已于同日回退为默认 260**（仓库+实机均恢复，插件已重载）。
- 诉求（改为 300）仍记录于此，等用户明确授权后执行。
- 执行涉及：两处 default 260→300 + 同步实机副本 + 重载插件；若配置页保存过显式旧值需另行确认。

### T6：WebUI 单条记忆时间显示怪异（"2026-08-23 22:21:00.947509"）

**现状**

- `core/page_api_modules/memory_handler.py:396` 直接透传 `metadata.create_time` / `updated_at` 原始值、不做格式化。
- 用户看到的 `2026-08-23 22:21:00.947509` 是 **datetime 对象被 JSON 序列化**成带微秒的字符串（存量数据）；当前写入点均为 `time.time()` 浮点（如 `memory_handler_update.py:114/270`）——浮点新数据与字符串存量混着显示，格式不统一。

**已查明（2026-08-24 核实）**

- **现生产代码无 datetime 写入点**：新增走 `add_canonical_memory`（`memory_engine_crud.py:278-314`，`time.time()` 浮点，普通新增强制重设 create_time）；更新走 `memory_handler_update.py:114/270`（`time.time()`）；replace 路径 `memory_engine_crud.py:1061-1062` 保留 create_time 为浮点。
- 用户看到的微秒字符串（`2026-08-23 22:21:00.947509`）是**早期版本写入的存量数据**（datetime 对象被 JSON 序列化成字符串）；`memory_handler.py:396` 把 `metadata.create_time` 原始值直接透传、不做格式化，浮点与字符串混着显示。

**下一步**

- PageAPI 输出端统一格式化（兼容浮点时间戳与字符串存量），或前端展示层兜底；写入端无需改。

### T7：LLM 返回 0 直接跳过，本应重试（bug）

**已核实（2026-08-24）**

- `_call_llm_with_retry`（`memory_processor.py:293-329`）仍只对**异常**重试（3 次指数退避）。
- LLM **正常返回但空结果/0 条获准事实**时直接跳过：`memory_processor.py:533`（"本窗口没有获准保存的事实，跳过 N 条候选"）、`memory_recall.py:303`（"相关候选均未通过注入预算，跳过"）——不重试。
- 影响：LLM 偶发返回空会导致本轮记忆丢失。bug 原样，未修。

**方向**：空结果也触发有限重试（1-2 次），或标记窗口待重试，与现有 `retry_count` 机制衔接（`memory_reflection.py` 已有待重试记录设施）。

### T8：记忆工具与自动总结产出的人物在图谱不复用（bug）

**现状**

- 自动总结路径身份键=`平台:sender_id`（`_extract_participant_identities`，`memory_processor.py:668-704`）。
- agent 显式记住路径走 `build_explicit_memory_record`（`memory_processor_build.py:325-418`），participant 身份用纯名字哈希（`person_<hash>`、`identity_key: None`），**不挂真实 sender_id/平台**——工具创建的 "T-White" 与自动总结的 "T-White" 身份键不同，图谱两个节点。

**已查明（2026-08-24 核实）**

- 工具路径 `build_explicit_memory_record`（`memory_processor_build.py:410-418`）的 participant_refs 用 `participant_id(scope, name)`（`person_<hash>`）、`identity_key: None`、`source: "explicit"`——**完全不挂真实发送者身份**。
- 自动总结路径 `_extract_participant_identities`（`memory_processor.py:669-704`）身份键=`平台:sender_id`，`_resolve_participant`（`memory_processor_build.py:179-194`）输出 `participant_id=identity_key`，图谱节点为 `account:<identity_key>`。
- 两套身份键不同：同一人物在工具路径产出 `person_<hash>` 节点、自动总结产出 `account:平台:sender_id` 节点 → **图谱两个节点**。确认为 bug，未修。

**下一步**

- 工具路径回填真实会话发送者身份（`平台:sender_id` + display_name，event 可拿到），或建立别名映射把 LLM 给的名字对齐已有身份节点。

## 状态词

```text
Deferred（已确认、暂不执行） | In Progress（动手了） | Done（清理完成）
```