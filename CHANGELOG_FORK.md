# LivingMemory Enhancement Fork 变更记录

本文件只记录 [2718labs fork](https://github.com/2718labs/astrbot_plugin_livingmemory) 相对[原版 LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)产生的变更。原项目自身的版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## [Unreleased]

### S0：逐事实准入

- 自动总结改为逐条输出候选事实及 `store / skip` 决定，程序统一推导窗口的 `store / skip / invalid` 结果。
- 候选事实先按同一事件收束到窗口结束时的最终状态：后续否认、纠正、澄清和约定覆盖中间误解，不再把已被推翻的说法另存为事实。
- 增加“本窗口新证据”边界：Bot 自己复述的旧记忆、人格设定和未经用户确认的过去叙述不能自我复制为新记忆；单次称呼、动作及窗口内反复出现的同一个玩笑也不自动升级为长期偏好或持续问题。
- `store` 改以跨对话复用价值判断，默认 `skip`；稳定身份或偏好、关系边界或约定、待办计划、持续问题、重要事件及明确记忆请求才进入长期候选。
- 增加严格 JSON 结构检查；格式失败只允许一次不改写事实的修复，仍失败时不写入、不生成兜底记忆。
- 混合窗口只把获准事实投影给现有存储和下游；全跳过时零写入并正常推进滑窗，结果无效时保留窗口进入原有重试路径。
- 自动总结与手动总结接入同一结果契约；补充中、英、俄三种手动跳过提示。
- 新增与生产顺序一致的 `top_k=4 + recent=2 + 最终注入` 基线，以及逐事实准入、格式修复和窗口状态回归测试。
- 使用本轮 23 条逻辑消息样本实测默认 `deepseek-v4-flash`：最终只保留用户明确表达的互动偏好及 Bot 接受的约定；改名测试、猫娘旧梗和单次亲昵称呼均未进入长期记忆。

### S1：v3 记忆产物契约

- 自动总结改为 `memories[]` 多产物契约：同一来源窗口可生成零条、一条或多条单中心记忆，并由自动/手动总结入口逐条写入。
- `key_facts` 升级为唯一的对象化事实真源；每条 fact 保存稳定 ID、parent 回链、自身 topic/participant、时间、重要度、来源消息和可选人格反应，不持久化第二份字符串 facts。
- `summary` 与 canonical content 由获准 facts 派生，新记录不再保存长篇 `persona_summary`。
- 明确区分事实事件时间与消息发送时间：`time.raw` 只能摘自消息正文，消息头时间只负责换算相对时间；正文无时间表达时写 `null`，来源时间仍由程序单独保存。
- 增加同 scope topic 精确复用、稳定来源指纹和 active 记忆幂等写入；OFFSET 仅保留为处理过程信息，不再作为长期来源边界。
- 为旧 Atom、图谱、注入和 WebUI 消费者增加只读 fact-text 投影，避免对象 facts 被错误字符串化；完整事实索引、图关系和其他写入入口统一仍留在 S2/S3。
- 新增多中心拆分、来源时间、稳定 ID、topic 复用、fact 级实体、幂等写入和真实数据库链路回归。

### S2：统一 canonical 写入与事实索引

- 自动总结、手动总结、主动记忆工具和直接 v3 写入统一进入一条 canonical pipeline；主动记忆不再生成 v2 字符串 facts。
- 新增独立 `memory_parents` / `memory_facts` 存储：parent 仅保存来源与 fact IDs，完整对象 facts 成为唯一持久事实真源；详情读取时按需重组 LM 风格 `key_facts`。
- 新增逐 fact FTS 和独立 FAISS 向量索引；搜索正文排除 sibling facts、长 summary 与 persona reaction，并保留未接入生产召回的候选查询接口供 S5 对照。
- 索引重建只从 active canonical facts 重放；canonical 构建未全部成功时父文档不会进入 active，写入失败会清除 document、parent、fact 和事实索引残留。
- 新 canonical 链暂不生成图或 Atom，分别留待 S3、S4；未增加实例旧数据迁移、清理或补造逻辑。
- 补充主动记忆契约、fact 搜索正文、真实 SQLite/FAISS 写入、双路候选、干净重建和故障零残留测试。

### S3：按事实证据重建 topic 与图谱

- 图谱只消费 S1/S2 canonical facts 的显式 topic/participant 绑定建边；删除 legacy 的 topic×fact、person×fact、person×person 全组合边，legacy 文档不再补造图边。
- topic/person 节点改用 S2 稳定 ID（scope + 规范化名称），跨记忆同概念单节点复用；仅字符级规范化，不做近义合并。
- `graph_edges` 新增多来源 `evidence`（fact_id/parent_id/source_message_ids）：删除一条记忆只移除自己的证据，最后来源消失才回收边与无引用节点。
- canonical 写入接回图索引并与全量 rebuild 共用同一构建器与稳定节点 ID；`rebuild_graph_index` 从 canonical facts 重放，在线/重建图签名一致。
- `graph_route_weight <= 0` 时图路线真正旁路：不创建双路检索器、不查询图、无零贡献候选占位。
- 固定 I18 回归样本（弱图路线第一名被归一化为满信号）与零权重旁路正确行为；融合公式修复留待 S5。
- 图谱页首次进入只加载受限概览；`full_graph=true` 仅在显式点击“全量图谱”按钮时发送。
- 合成多会话样本结构验收全 PASS（零全组合、全边带证据、共享节点复用、删除安全、间接联想路径可达）；语义级独有命中由 S5 判定，生产图权重保持 0。

## [2.6.0-a1] - 2026-08-20

### 项目

- 以原项目提交 `c2e7330` 为改造基线，使用 `refactor/enhancement` 作为 fork 的默认开发分支。
- 明确当前主线：依次改造记忆总结、处理与存储、事实与图谱、召回与注入；先处理确定的缺陷和严重结构问题，新增能力后置。
- 将 T0 调查底稿和 S0–S6 执行路线图纳入仓库 `docs/livingmemory-roadmap/`，作为当前唯一实现计划。
- 在 README 开头补充 fork 定位，并将 fork 变更与原项目更新记录分开维护。

### 变更

- 将 `graph_memory.dynamic_route_weighting` 的默认值从 `true` 改为 `false`；已有配置显式设为 `true` 时仍可启用。动态路线选择是否值得重新开启，留待主链稳定后单独评估。
