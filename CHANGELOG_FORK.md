# LivingMemory Enhancement Fork 变更记录

本文件只记录 [2718labs fork](https://github.com/2718labs/astrbot_plugin_livingmemory) 相对[原版 LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)产生的变更。原项目自身的版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## [2.6.0-a3] - 2026-08-24

- 重写自动总结的输出契约：增加一份完整的 topic 候选、对话输入和嵌套 JSON 输出示例，并单独展示可选 `persona_reaction` 的正确插入位置，降低结构误解和字段照抄。
- 明确同一事件只保留窗口结束时已确认的最终状态；Bot 复述不算新用户事实，同一事件的确认、情绪与纠正合并为一条 fact。
- 为 fact 级 `importance` 增加 0.0–1.0 的分档参照；评分只描述未来参考价值，不增加代码侧存储阈值。
- 自动总结可见每条消息的完整时间；本窗口事实在正文中固定具体日期、自然描述时段，相对日期按对应消息换算，外部事件时间仍以事件本身为准。
- 在 Sfuture 中加入“从同一人物的父记忆及事实提炼可追溯用户偏好画像”的远期愿景，本版本不实现画像状态或消费链。

## [2.6.0-a2] - 2026-08-23

### 2026-08-23 收尾波次：recent 短期连续性、预算默认值与泄漏修复

- 新增最近记忆块（S-recent）：48 小时窗口内的最新 parent 摘要无条件注入，并带最多 2 条词面相近 facts（宽松阈值 = `fact_min_lexical_score × 0.6`）；recent 条目不占 `top_k` 名额，与主召回共用同一 token 预算。窗口、条数与开关均可配置（`recent_block_enabled` 默认开、`recent_block_window_hours` 默认 48、`recent_block_max_facts` 默认 2）。
- 注入预算默认值调整并定稿：总预算 `injection_token_budget` 1200→1600（校验 128–16000），单条 fact `single_fact_token_budget` 经 320→150→320 修正后定稿 260（校验 32–8000）；配置页 schema 与 i18n 同步。
- Agent 工具描述改为用法优先：`memory_search` 描述重写（recent 对话已自动提供、查关键词、k 默认 5），`memory_memorize` 描述压缩回原版长度；工具 JSON 与 fake_tool_call 输出精简对齐上游结构（去掉 fact_id/parent_id/last_access_time/预算字段）。
- 泄漏修复：总结 prompt 只给 topic 可见名、不再暴露 topic_id；`_resolve_topic` 拒绝 `topic_<hex>` 机器名（回退候选池人话名或丢弃），防止 LLM 把内部 ID 当名字写入图谱。
- 注入前缀恢复：每条 fact 注入带「记忆 #N (重要性: X.XX, 写入时间: YYYY-MM-DD HH:MM)」前缀，recent 条目渲染「最近对话 #N」；重要性读取 `memory_facts.importance` 当前衰减值。
- 测试：新增真实落库 memorize 端到端测试（`test_memorize_tool_real_db.py`）、recent block 6 用例、机器 ID topic 写入回归；Python 816 / 前端 16 全绿。

### Stest：只读实例重放与架构校对（完成）

- 精简自动总结契约：LLM 只输出获准 facts 的正文、topics、importance 及可选 reaction；summary、participants、文档 topics/importance、ID 和窗口来源均由程序生成。删除 fact 级 `source_indexes/source_message_ids` 及其伪语义校验，内置提示词不再重复完整规则。
- 新增只读实例重放与自然问法探针：从保留原始消息的旧窗口重跑 v3 写入链，只在隔离目录生成候选数据库、FTS/FAISS 索引和私有报告，不迁移或替换运行实例。
- 第一轮 16 个窗口（324 条消息）生成 24 条 canonical facts；事实、FTS、向量索引保持 24/24/24 一致，四条无关查询均保持零注入。
- 评测发现并修复中文按 UTF-8 字节误算预算、中文停用词抹掉有意义原话等 S5 回归；另补齐“你在吗”轻量消息门和“来着”历史追问提示。
- 撤销“同一 parent 最多注入一条 fact”的二次限流：自动召回最终数量统一由现有 `top_k` 收口，同一来源窗口中多条独立且相关的 facts 可共同进入载荷，token 预算只处理超长而不替代条数设置。
- 修复多中心外壳把原版“十轮总结最多 5 个 facts”意外放大成最多 25 个的问题：保留每条 memory 最多 5 个 fact，并让整个窗口合计仍最多 5 个；同一事件、关系变化或结论的过程话语必须合并，超量回答只允许一次不补造信息的压缩修复，仍超量则不写入。
- 用两段私有十轮素材按现行契约实测：模型分别一次生成 4 个和 1 个 canonical facts，未再出现第一段超过十条的逐句拆分；隔离索引保持 5/5/5 一致，WebUI 列表与详情可读，真实追问的 3 条/1 条合格 facts 全部进入预算内载荷。
- 旧版同-parent限流下的固定自然问法成绩（Hit@k 23/24、目标最终注入 20/24）保留为历史记录，不再作为当前装配规则的验收结论；当前结果以本轮重新实测为准。
- 修正第一轮评测中过窄的“长期价值”标准，同时明确不为约定建立专用系统：长期约定可按普通 fact 入库；短期任务的主动唤醒由 Bot/框架已有能力负责，本 fork 不代为实现。
- 时间处理复用原版思路：相对时间直接改写为 fact 正文中的具体日期，消息发送时间只保留在来源元数据；撤销独立 `fact.time`、时间注入附加行和详情时间字段。结构化时间与按时间检索降入 `Sfuture`。
- 修正私聊提示词和路线图示例中的 Bot/user 身份表述；公开示例使用虚构姓名，当前 Bot 沿用原版的第一人称与 persona_id 隔离方式。
- 第一人称沿用原版提示约束，不再因模型使用 Bot 名称而由代码判废整批记忆；用户与 Bot 身份仍以消息角色、发送者身份和 persona_id 隔离。
- 对窗口 30、63、80、100 做针对性复跑：4 个窗口均形成有效结果，共写入 10 条 canonical facts，精确查询 10/10 进入最终注入，四条负例仍为零注入。窗口 30 不再把 Bot 单方面声称的“九点查岗”写成双方约定，窗口 63 的明确亲昵表达也没有再被整窗跳过；窗口 100 仍保留两条偏玩笑的“价目表/催睡”互动，说明写入取舍和跨次稳定性仍需后续重复评测。
- WebUI 改为以 canonical fact 为准：列表/详情显示权威 fact 数量和证据字段；v3 结构正文只读，避免旧表单把对象事实压成字符串；归档、恢复、软删除走真实 fact 生命周期；系统统计以 fact 状态和 FTS/向量一致性替代 Atom。
- 当前工作树通过 816 项 Python 测试和 16 项前端测试；Stest 时间压缩回放（25/25）与成对体验盲测（10 场景 7/2/1 偏好候选）均已通过，总判定为核心通过、可选路线保持关闭，详见 [Stest.md](docs/livingmemory-roadmap/Stest.md)。

### S0：逐事实准入

- 自动总结逐条判断事实，但只输出值得保存的结果；程序统一推导窗口的 `store / skip / invalid` 状态。
- 候选事实先按同一事件收束到窗口结束时的最终状态：后续否认、纠正、澄清和约定覆盖中间误解，不再把已被推翻的说法另存为事实。
- 增加“本窗口新证据”边界：Bot 自己复述的旧记忆、人格设定和未经用户确认的过去叙述不能自我复制为新记忆；单次称呼、动作及窗口内反复出现的同一个玩笑也不自动升级为长期偏好或持续问题。
- `store` 改以以后是否仍有助于理解人物、关系或事情判断，默认 `skip`；稳定身份或偏好、关系边界、已接受的长期约定、持续问题、重要事件及明确记忆请求可进入候选。
- 增加严格 JSON 结构检查；格式失败只允许一次不改写事实的修复，仍失败时不写入、不生成兜底记忆。
- 混合窗口只把获准事实投影给现有存储和下游；全跳过时零写入并正常推进滑窗，结果无效时保留窗口进入原有重试路径。
- 自动总结与手动总结接入同一结果契约；补充中、英、俄三种手动跳过提示。
- 新增与生产顺序一致的 `top_k=4 + recent=2 + 最终注入` 基线，以及逐事实准入、格式修复和窗口状态回归测试。
- 使用本轮 23 条逻辑消息样本实测默认 `deepseek-v4-flash`：最终只保留用户明确表达的互动偏好及 Bot 接受的约定；改名测试、猫娘旧梗和单次亲昵称呼均未进入长期记忆。

### S1：v3 记忆产物契约

- 自动总结改为 `memories[]` 多产物契约：同一来源窗口可生成零条、一条或多条单中心记忆，并由自动/手动总结入口逐条写入。
- `key_facts` 升级为唯一的对象化事实真源；每条 fact 保存稳定 ID、parent 回链、自身 topic/participant、重要度和可选人格反应，不持久化第二份字符串 facts 或 fact 级来源编号。
- `summary` 与 canonical content 由获准 facts 派生，新记录不再保存长篇 `persona_summary`。
- 事实中的相对时间按消息语境直接改写为具体日期；消息发送时间仍由程序单独保存为来源元数据，不建立独立事件时间字段。
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
- `graph_edges` 新增多来源 `evidence`（source_memory_id/fact_id/parent_id）：删除一条记忆只移除自己的证据，最后来源消失才回收边与无引用节点。
- canonical 写入接回图索引并与全量 rebuild 共用同一构建器与稳定节点 ID；`rebuild_graph_index` 从 canonical facts 重放，在线/重建图签名一致。
- `graph_route_weight <= 0` 时图路线真正旁路：不创建双路检索器、不查询图、无零贡献候选占位。
- 固定 I18 回归样本（弱图路线第一名被归一化为满信号）与零权重旁路正确行为；融合公式修复留待 S5。
- 图谱页首次进入只加载受限概览；`full_graph=true` 仅在显式点击“全量图谱”按钮时发送。
- 合成多会话样本结构验收全 PASS（零全组合、全边带证据、共享节点复用、删除安全、间接联想路径可达）；语义级独有命中由 S5 判定，生产图权重保持 0。

### S4：收敛 Atom，停用独立事实层

- 完成 A/B 对照（合成脱敏样本，纯离线可复现）：canonical fact store 与独立 Atom 在分类、时间基准、检索命中、负注入、过期语义和存储成本六维对比，Atom 无任何生产净收益。
- 规则分类器可复现错分类（过去事件被标成 `planned`，样本错分类率 40%）；Atom 时间基准用写入时刻而非源消息时间戳（同一句“上周六”差 14 天）；中文句子查询在 Atom FTS 下 3/3 落空，canonical fact FTS 3/3 命中。
- 决策为方案 B：canonical fact store（`memory_facts` + 可重建 FTS/向量投影）成为唯一生产事实层；独立 Atom Retriever、FTS、分类器和定时生命周期全部停用。
- `MemoryEngine` 不再初始化 AtomStore/AtomLifecycleManager/AtomRetriever；`atom_enabled` 默认改 `false` 且不再驱动任何初始化。
- `replace_memory`、`restore_memory` 和 Page API 导入入口停止生成 Atom；`GraphExtractor` 删除 `_extract_from_atoms` 建图路径，`GraphMemoryManager` 移除 atoms 参数链。
- 不迁移、不清洗实例旧 Atom 数据；新链路不再创建 `memory_atoms` 表，生产检索路径（从未经过 Atom）前后一致。
- 对照脚本 `code/scripts/s4_atom_eval.py` 与报告 `code/artifacts/s4_atom_eval_report.json` 全 PASS；新增停用行为回归测试（初始化三组件为 None、replace/restore 不生成 Atom、extract 忽略 atom 载荷）。

### S5：事实级召回与预算注入

- 生产 `search_memories()` 从十轮 document 切换为 canonical fact；命中事实只回链 parent 来源，不再默认带回整篇 summary、同 parent 其他 facts、topics 或评分元数据。
- 增加轻量消息前置空结果和搜索后相关性拒绝；recent 不再预留固定槽位，也不能绕过事实相关性。
- 候选路线保留词面、向量、图的绝对原始信号；修复 document/graph 各自按本路线最高分归一化造成弱路线满信号的问题，图路线默认权重保持 0。
- 自动召回与 Agent 主动召回共用完整事实 packer：候选数量与最终注入数量分离，总预算和单事实预算均可配置，放不下时停止而不截断；persona reaction 作为独立短句计入同一预算。
- 召回调试接口与页面显示候选/最终数量、预算、路线、事实 ID 和拒绝原因；调试查询不改变事实生命周期。
- 修复 canonical 图写入缺少实际 session/persona 作用域的问题，使显式启用的图路线能在事实召回中命中同作用域证据。
- 新增固定正负样本评测、弱图回归、多事实 parent 隔离、预算和真实 SQLite/FAISS 事件注入测试。

### S6：事实生命周期与必要校对体验

- canonical fact 增加 `retrieved` 与 `injected` 两套时间/次数；候选命中不再等同实际使用，只有真正送入模型的事实影响后续衰减。
- 每日衰减、重要度更新、归档、恢复和删除统一作用到事实真源及其 FTS/向量投影，父 document 不再绕过事实状态召回；批量删除同时清理 canonical 子记录。
- 暂不实现 `adopted`：当前没有可靠信号证明模型回答实际采用某条事实，因此不建立空字段或伪计数。
- 记忆详情页将 fact、persona reaction、状态、检索次数和实际注入次数分开展示；注入提示缩为“相关历史作背景、当前消息优先、自然使用且不主动报菜名”的短契约。
- 事实涉及时间时直接在正文显示具体日期；约定与其他普通事实共用同一召回和衰减规则，不增加自动完成、催办或衰减豁免。
- S0–S6 完成后将 `Stest` 标记为 Ready；整体 A/B、长周期回放与体验盲测仍需独立运行，当前结果不冒充最终净收益结论。

## [2.6.0-a1] - 2026-08-20

### 项目

- 以原项目提交 `c2e7330` 为改造基线，使用 `refactor/enhancement` 作为 fork 的默认开发分支。
- 明确当前主线：依次改造记忆总结、处理与存储、事实与图谱、召回与注入；先处理确定的缺陷和严重结构问题，新增能力后置。
- 将 T0 调查底稿和 S0–S6 执行路线图纳入仓库 `docs/livingmemory-roadmap/`，作为当前唯一实现计划。
- 在 README 开头补充 fork 定位，并将 fork 变更与原项目更新记录分开维护。

### 变更

- 将 `graph_memory.dynamic_route_weighting` 的默认值从 `true` 改为 `false`；已有配置显式设为 `true` 时仍可启用。动态路线选择是否值得重新开启，留待主链稳定后单独评估。
