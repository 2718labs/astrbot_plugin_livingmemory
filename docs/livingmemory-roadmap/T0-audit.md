# LivingMemory T0 全链路调查地图与改造路线图

> 日期：2026-08-20  
> 工作区：`lm-fork-enhancement`  
> 调查基线：官方上游 `master@c2e733049392d1cfc27843fc083096a9103f27d1`  
> 当前版本标识：`2.6.0-beta.3-2-gc2e7330`  
> 文档状态：T0-A、T0-B 调查已收口；后续路线图主稿  
> 维护方式：只维护本 Markdown；旧 Excel 停止更新  

> 2026-08-20 更新：本文保留为完整调查与证据档案。后续执行状态改由 `livingmemory-roadmap/README.md` 和 `S0.md`–`S6.md` 维护。

## 1. 文档定位

本文接替调查期契约，承担两件事：

1. 固化 T0-A、T0-B 的真实生产链地图；
2. 把已经核实的问题按依赖和风险排成可逐步执行的改造路线。

四份既有材料只作为历史线索，不作为当前代码事实：

| 参考材料 | 本轮用法 | 注意事项 |
|---|---|---|
| `memory-system-analysis.md` | 提供召回、图谱、Atom、注入等候选问题 | 基于较早代码与样本，结论需逐项复核 |
| `foundation-audit.md` | 提供底层一致性与检索问题清单 | “动态权重有害”没有稳定证据，不进入问题表 |
| `memory-system-overhaul.md` | 提供图路线、注入提示等改造设想 | 现行自定义提示已变化，旧 C2 结论需改写 |
| `fork-plan.md` | 提供旧阶段拆分思路 | 文件结构、行号和阶段划分已经过期，不沿用旧阶段编号 |
| `livingmemory-investigation-plan.md` | 本轮 T0-A / T0-B 调查契约 | 调查项已在本文收口，仍保留作证据标准 |

本文不包含代码改造。生产代码、运行数据库和配置均未写入。

---

## 2. 证据、分类与优先级规则

### 2.1 真实性等级

| 等级 | 含义 | 可用于什么结论 |
|---|---|---|
| E1 | 当前基线代码路径 + 定向运行复现 | 可确认具体缺陷真实存在 |
| E2 | 当前基线代码路径 + 当前只读运行状态 | 可确认机制存在且当前实例暴露于该机制 |
| E3 | 当前基线代码静态可达 | 可确认设计风险；未声称当前数据已受损 |
| E4 | 2026-08-19 离线评估样本 | 只用于说明该样本中的质量或性能表现，不外推为普遍结论 |
| H | 历史文档主张，当前未获支持或已被修正 | 不进入实施依据，保留作背景 |

### 2.2 类型与优先级

| 标记 | 定义 | 排序原则 |
|---|---|---|
| FIX | 当前行为违背已有配置语义、数据一致性或既有机制承诺 | 最先处理 |
| OPT-S | 严重优化；系统能运行，但结构会持续放大错误、成本或不可控性 | 在 FIX 后处理 |
| FEAT | 新能力，不是修复既有承诺 | 在基础正确后处理 |
| UX | 展示、文案、可观察性和使用体验 | 最后处理 |
| P0 | 可造成跨记录数据错误或不可逆丢失 | 立即阻断后续依赖改造 |
| P1 | 主链正确性、幂等性、恢复一致性或生产开销存在高风险 | P0 后优先 |
| P2 | 明显影响召回质量、治理语义或长期可维护性 | 完成关键 FIX 后处理 |
| P3 | 体验和可观察性问题 | 靠后 |

### 2.3 当前运行快照

以下只表示调查时刻的本地实例，不等于上游默认值：

| 项目 | 当前值 / 状态 |
|---|---|
| 自动召回 | `top_k=4`，`max_k=8` |
| 文档最低重要性 / 相似度 | `0.3 / 0.3` |
| 最近记忆保留 | 2 条，60 小时内 |
| 注入位置 | `extra_user_content` |
| 图谱 | 已启用；文档权重 `0.65`；图权重 `0`；动态权重关闭 |
| Atom | 已启用 |
| Agent 搜索 / 主动记忆工具 | 均启用 |
| Consolidation | 已启用；每日；按 session；至少 3 条；最多 5 组 |
| 文档 | 77 条：70 active，7 archived |
| Atom | 302 条：246 active，24 expired，32 forgotten，0 superseded |
| 图 | 567 nodes，1781 edges，2504 entries |
| 写操作修复队列 | pending / needs_repair 均为 0 |
| 当前精确重复正文 | 按正文 + scope 统计为 0 组 |
| 当前孤立 Atom / 缺边 entry | 均为 0 |

当前数据“没有发现现成损坏”不能反证缺陷不存在；其中共享边删除与零权图路线已经由当前代码定向复现。

---

## 3. 一页结论

| 结论 | 真实性 | 分级 | 处置方向 |
|---|---|---:|---|
| 主写入链真实可达：消息进入 ConversationStore，反思触发总结，经 MemoryProcessor 后写文档、索引、Atom、图与来源 | E2 | 已确认链路 | 作为后续回归基线 |
| 主召回链真实可达：当前消息查询文档与图，融合、过滤、追加最近记忆后注入当前请求 | E2 | 已确认链路 | 作为后续回归基线 |
| 共享语义边只有单一 `source_memory_id`；删除该来源会连带删除其他记忆对同一边的 entry | E1 | P0 / FIX | 第一优先级修复并审计现有图 |
| `graph_weight=0` 仍运行图路线，图独有结果可用 0 分占据 `top_k` | E1 + E4 | P1 / FIX | 权重为 0 时硬旁路；融合前剔除无贡献候选 |
| 自动总结没有稳定来源键和提交幂等；写成功、游标更新失败时存在重复写风险 | E3 | P1 / FIX | 改用稳定消息 ID / source fingerprint + 唯一约束 |
| `source_window` 使用会被裁剪重排的 OFFSET 坐标，不能长期证明记忆来源 | E2 | P1 / FIX | 持久化首尾消息 ID、数量与摘要指纹 |
| 解析兜底或低质量总结仍作为 active 文档进入召回 | E2 | P1 / FIX | 写入前质量门；失败转隔离态而不是 active |
| 在线建图、恢复建图与全量重建使用的输入语义不等价 | E3 | P1 / FIX | 建立统一的“标准记忆中间表示”并让所有入口复用 |
| Consolidation 分组没有组内条数上限，且“先写新、再归档旧”不是原子替换 | E2 | P1 / FIX | 加组内上限、状态机、可恢复提交协议 |
| Atom 的过期、遗忘不会抑制父文档召回；AtomRetriever 未接入生产召回 | E2 | P1 / FIX | 先决定 Atom 的权威边界，再接通或停用死机制 |
| 每个事实 Atom 继承整组 topics / participants，图关系仍接近笛卡尔积 | E2 | P2 / OPT-S | 让 LLM / 规则输出事实级实体绑定，限制弱边 |
| 最近记忆硬占槽且不参加统一重排；负向查询没有 abstain | E2 + E4 | P2 / OPT-S | 统一候选池、相关性门槛、空结果契约 |
| 命中即更新访问次数，衡量的是“检索返回”而非“实际注入 / 使用” | E3 | P2 / OPT-S | 分离 retrieved、injected、adopted 三类事件 |
| 注入没有总 token / 字符预算，摘要、事实和元数据可能重复放大 | E3 | P2 / OPT-S | 注入预算器、去重、按字段裁剪 |
| 现行自定义提示已经要求不复述、忽略无关记忆、当前对话优先 | E2 | 旧结论已修正 | 不再作为严重 FIX；只保留后续 UX 微调 |
| SUPERSEDED 仅有枚举，没有生产写入；矛盾检测尚未形成闭环 | E2 | FEAT | 基础修复后再建设 |

---

## 4. T0-A：源头 → 总结 → 处理 → 存储 → 图谱

### 4.1 主链地图

```text
用户消息 ─┬─ 私聊：on_llm_request 前写入 ConversationStore
          └─ 群聊：被动捕获器写入 ConversationStore

模型最终回复 → on_llm_response → 写入 ConversationStore
                               → 计算未总结轮数
                               → MemoryProcessor 调用 LLM 总结
                               → JSON 解析 / 修复 / 兜底
                               → 标准文档 + 规则 Atom
                               → MemoryEngine.add_memory
                                    ├─ documents / FAISS
                                    ├─ BM25 FTS
                                    ├─ memory_atoms / Atom FTS
                                    ├─ graph nodes / edges / entries / graph vector
                                    └─ memory_sources
                               → 成功后推进总结游标
```

### 4.2 调查表

| ID | 模块 / 问题 | 当前真实行为 | 边界与失败行为 | 结论 / 关联问题 |
|---|---|---|---|---|
| A01 | 插件初始化 | 插件注册后异步初始化 DB、FAISS、图 DB、引擎、会话管理器、Processor、Consolidation 与调度器 | 核心就绪后才构造事件处理器 | 主链可达，E2 |
| A02 | 私聊用户消息源 | 自动召回前先把当前用户消息写入会话存储 | 当前消息会成为后续总结素材 | 正常基线 |
| A03 | 群聊消息源 | 被动捕获所有合格用户消息，跳过机器人回声 | 与是否触发 LLM 回复分离 | 正常基线；需保留群聊 sender |
| A04 | 助手消息源 | 只在最终 LLM 响应阶段落会话消息 | 工具调用、工具摘要、错误响应被排除 | 正常基线 |
| A05 | 会话存储字段 | `messages` 有稳定整数 ID、session、role、content、sender、时间 | 后续总结却按 OFFSET 读取 | I03 / I04 |
| A06 | 自动触发 | 默认累计 10 轮未总结对话触发；同 session 只允许一个在途任务 | 以未总结消息数除 2 估算轮数 | 触发可达，E2 |
| A07 | 总结任务并发 | session 级在途锁避免同一时刻重复启动 | 不能覆盖“写成功后游标失败 / 重启”的幂等窗口 | I03 |
| A08 | 待处理重试 | 失败最多重试 3 次；第 3 次后推进游标并丢弃该范围 | 避免永久阻塞，但会永久跳过未总结内容 | I05 的失败面 |
| A09 | 总结输入 | 按 session、start_index、end_index 使用 OFFSET / LIMIT 读取 | 头部裁剪后坐标会移动、复用 | I04 |
| A10 | Prompt 来源 | 自定义总结 prompt 优先，其次内置，最后硬编码兜底 | provider 每次调用时动态解析 | 正常基线 |
| A11 | LLM 调用 | 最多 3 次指数退避；区分群聊 / 私聊 prompt，并注入 persona | 最终失败交给反思重试策略 | 正常基线 |
| A12 | 解析 | 首先解析 JSON，失败后修复，再正则兜底与字段默认 | “能解析”不代表内容质量足够 | I05 |
| A13 | 质量标记 | 低质量只写标签，不阻止写入 | 当前库存在 8 条低质量文档 | I05，E2 |
| A14 | 文档构建 | 正文为 summary + key_facts；元数据保存 topics、facts、participants、sentiment、interaction、persona 等 | 部分字段在正文和元数据重复 | I14 |
| A15 | Atom 构建 | 每条 key_fact 生成一条规则分类 Atom，推导类型、置信度和 TTL | 每条 Atom 都拿到整组 topics / participants | I09 |
| A16 | 文档写入顺序 | 先 document / FAISS，再 BM25；BM25 失败会回滚文档和向量 | 有 write-op 日志供启动时修复 | 基础补偿存在 |
| A17 | Atom / 图写入 | 文档索引成功后写 Atom，再写图；失败标记 `needs_repair`，文档保留 | 当前修复队列为 0，不代表未来无风险 | 正常基线 + I06 / I09 |
| A18 | 来源保存 | 最后写 `memory_sources`；来源失败会回滚文档 | `source_window` 只有 session 与可变索引 | I03 / I04 |
| A19 | 图谱构建 | 在线自动总结优先从 Atom 建图；legacy 路径按 topics、facts、persons 组合 | 每 Atom 携带全量实体，因此仍产生大量弱关系 | I09 |
| A20 | 成功提交 | `add_memory` 成功后才推进会话总结游标 | 两者不是同一事务；中间崩溃可重复写 | I03 |

### 4.3 写入旁路与恢复路径

| 入口 | 是否走 MemoryProcessor | 是否生成 Atom | 是否建图 | 与自动总结是否等价 |
|---|---:|---:|---:|---|
| 自动反思总结 | 是 | 是 | Atom 路径 | 基准路径 |
| `/lmem summarize` | 是 | 是 | Atom 路径 | 大体等价，但可选择范围并重置游标 |
| Agent memorize 工具 | 否 | 否 | legacy metadata 路径 | 不等价 |
| Consolidation 新记忆 | 自身合并流程 | 否 | legacy metadata 路径 | 不等价 |
| 归档后恢复 | 否；从 metadata 重建 | 重新规则分类 | Atom 路径 | 取决于 metadata 是否完整 |
| 图谱全量重建 | 否；读取 documents | 不重建 Atom 作为输入 | legacy metadata 路径 | 不等价 |
| 导入 / API 路径 | 依入口而异 | 不保证 | 依 metadata | 不等价 |

结论：目前“同一条记忆”会因为进入系统的入口不同而获得不同的 Atom 与图结构；重建也不能保证恢复出在线写入时的图。这不是单纯测试缺口，而是输入契约未统一。

### 4.4 存储与一致性矩阵

| 存储层 | 写入者 | 主要消费者 | 删除 / 归档 | 关键风险 |
|---|---|---|---|---|
| `messages` | 会话捕获 | 总结器 | 头部裁剪 | 稳定 ID 未进入 provenance |
| `documents` | FAISS 文档存储 / MemoryEngine | 文档召回、详情、重建 | 删除或 status=archived | 父文档是实际召回权威 |
| 文档向量索引 | HybridRetriever / FAISS | 文档向量召回 | 删除；归档尝试独立删向量 | 多存储提交依赖 write-op 补偿 |
| 文档 FTS | BM25Retriever | 文档关键词召回 | 删除或归档移除 | 与 documents 需同步 |
| `memory_atoms` + FTS | AtomStore | 生命周期；当前无生产召回消费者 | 删除 / 归档一起清除；恢复重建 | 生命周期不控制父文档召回 |
| 图 nodes / edges / entries | GraphMemoryManager | 图关键词与图向量召回 | 删除 / 归档清理；恢复重建 | 共享边所有权模型错误 |
| 图向量 | GraphVectorStore | 图语义召回 | 随图维护 | 即使权重 0 仍可能执行查询 |
| `memory_sources` | MemoryEngine | 来源追踪 / 审计 | 随文档删除 | 坐标不稳定、缺少幂等键 |
| `write_ops` | MemoryEngine | 启动修复 | 完成后保留状态 | 能补偿部分多存储失败，不等于全局事务 |

### 4.5 图共享边缺陷复现

当前图存储会把语义相同的边合并成一个 edge 行；该行只保存最早的 `source_memory_id`。其他记忆通过 entries 指向同一个 edge。

定向复现结果：

```text
两条记忆写入同一语义边
before entries = 2
删除最早来源记忆后 after entries = 0
第二条仍存在的记忆 graph entries = 0
```

当前只读数据库已经存在 56 个跨来源 entry 链接，分布在 3 条共享边上。这说明缺陷存在现实暴露面；调查时尚未执行删除，因此不声称当前数据已损坏。

---

## 5. T0-B：召回 → 排序 → 注入 + Atom / 生命周期 / Consolidation

### 5.1 主链地图

```text
当前用户消息
  → on_llm_request 自动召回
  → resolve_memory_scope
  → MemoryEngine.search_memories
       ├─ 文档路：BM25 + vector → RRF → 权重 → MMR
       └─ 图路：graph FTS / 展开 + graph vector → 图内融合
  → DualRouteRetriever 跨路归一化 / 加权 / bonus
  → status / importance / signal / event 过滤
  → 合并 recent memories
  → 异步增加文档 access_count
  → FormattingAdapter 格式化完整父文档
  → extra_user_content 注入当前模型请求
```

### 5.2 调查表

| ID | 模块 / 问题 | 当前真实行为 | 边界与失败行为 | 结论 / 关联问题 |
|---|---|---|---|---|
| B01 | 自动召回入口 | `on_llm_request` 使用当前用户消息查询并修改当前 ProviderRequest | 不是只存在类；会影响当前回复 | 生产可达，E2 |
| B02 | 查询文本 | 默认就是当前消息；可选最近对话扩展在当前实例关闭 | 无独立 query planner | 正常基线 |
| B03 | Scope | 先解析会话 / 用户作用域，可附带 persona | 所有当前 active 文档处于同一 scope | 正常基线 |
| B04 | 文档路 | BM25 与 vector 并发，RRF 融合，应用权重和 MMR | 真实主召回路径 | 生产可达 |
| B05 | 图路 | 图 FTS / 节点展开与图向量融合成图候选 | graph enabled 时总会启动 | I02 |
| B06 | 双路执行 | 文档路、图路并发执行后做逐路最大值归一化 | 路线权重不是执行开关 | I02 / I13 |
| B07 | 零权语义 | `graph_weight=0` 时图路仍执行；图独有结果最终分为 0 仍可进入 top_k | 定向复现返回 `[(1,1.0),(2,0.0)]` | I02，E1 |
| B08 | 过滤 | active、importance、vector signal、event_only 等在融合后过滤 | 过滤晚于图查询，不能节省零权成本 | I02 |
| B09 | event_only | 只有在 `atom_types` 非空时才检查事件类型；无类型记录可通过 | 与“只要事件”字面语义不一致 | I12 |
| B10 | 最近记忆 | SQL 取最新 active 记录，赋 `final_score=1.0` | 无查询相关性要求 | I10 |
| B11 | 最近记忆合并 | 先给 recent 预留数量，再从排序结果填剩余槽 | 当前 top_k=4、recent=2，最多半数槽位硬保留 | I10 |
| B12 | Access 更新 | 对 search 返回的文档异步增加时间与次数 | 更新发生在模型是否实际使用之前 | I13 |
| B13 | 注入位置 | 默认 / 当前均为 `extra_user_content` 临时内容 | 确实被当前 LLM 消费 | 生产可达 |
| B14 | 注入内容 | 标题、重要性、写入时间、topics、participants、facts、persona summary / content | 整个父文档注入；字段可能重复 | I14 |
| B15 | 注入预算 | 没有总 token / 字符预算，也没有跨字段去重器 | top_k 增大或长文档会放大上下文 | I14 |
| B16 | 现行自定义提示 | 明确要求把记忆视为自身背景、不复述、不宣布、忽略无关项、当前对话优先 | 修正历史报告中的严重“诱导复述”判断 | 非 P1；保留 UX |
| B17 | Agent 搜索工具 | 默认与当前实例均启用，复用 `search_memories` | Agent 决定何时调用，但候选逻辑相同 | 生产可达 |
| B18 | AtomRetriever | 类、初始化与单测存在；生产代码只有构造，没有实际调用 | 不能影响自动召回和搜索工具 | I08，E2 |
| B19 | Atom 生命周期 | graph + atom 开启时启动循环；active → expired → forgotten → purge | 父 documents 仍 active、仍被召回 | I08 |
| B20 | Atom reinforcement | 方法存在，但生产链没有调用；Atom last_accessed 不更新 | 生命周期没有真实使用反馈 | I08 |
| B21 | SUPERSEDED | 状态枚举存在，生产路径无写入者 | 不是现有矛盾处理能力 | F01 |
| B22 | Consolidation 候选 | active、达到年龄、低于重要性阈值；按 session 或语义成组 | 当前实例每日启用 | I07 |
| B23 | Consolidation 分组 | session 模式可把同一 session 所有候选放一组；semantic 用连通分量 | 都没有组内记忆条数上限；只有每次组数上限 | I07 |
| B24 | Consolidation 提交 | 先新增合并记忆，再归档 / 删除旧记忆；新记忆不生成 Atom | 中途失败可并存新旧；建图语义与主链不同 | I06 / I07 |

### 5.3 Atom 实际状态机

```text
规则分类器写入 Atom
        ↓
      ACTIVE
        ↓ TTL 到期
      EXPIRED
        ↓ 遗忘周期
      FORGOTTEN
        ↓ 清理
       PURGED

SUPERSEDED：只有枚举，无生产写入路径
REINFORCE：有方法，无生产调用者
PARENT DOCUMENT：全程可继续保持 ACTIVE 并参与召回
```

因此，当前 Atom 生命周期更接近独立后台账本，不是记忆召回生命周期。不能因为 Atom 已过期就推断用户不再会召回对应记忆。

### 5.4 2026-08-19 离线评估校正

样本为 39 个正向查询、4 个负向查询。它只能说明该数据集上的相对表现：

| 变体 | Hit@1 | Hit@4 | Hit@8 | MRR | 平均耗时 |
|---|---:|---:|---:|---:|---:|
| live | 0.5128 | 0.8974 | 0.9744 | 0.6770 | 0.19s |
| live-static | 0.5128 | 0.9231 | 0.9744 | 0.6782 | 0.20s |
| no-graph | 0.5897 | 0.9744 | 1.0000 | 0.7726 | 0.03s |
| zero-graph | 0.6154 | 1.0000 | 1.0000 | 0.7821 | 0.19s |
| graph-only | 0.5385 | 0.8462 | 0.9487 | 0.6811 | 0.20s |

可下的结论：

- 在该快照上，图路没有带来净收益；
- 图权重为 0 时耗时没有消失，符合“仍执行图查询”的代码事实；
- 4 个负向查询在所有变体中都返回了 8 条结果，暴露缺少 abstain / 空结果契约；
- 动态权重与静态权重差异很小，不能据此认定动态权重有害。

---

## 6. 已确认问题登记表

下表是完整问题账本，不等于实施日程。编号稳定，后续更新状态时不要重排编号。真正的实施主线见第 7 节。

### 6.1 主线与护栏分层

| 层级 | 问题 | 如何处理 |
|---|---|---|
| 主线 | I05 总结质量、I08 Atom 无消费者、I09 图过连、I10 最近记忆硬占位、I11 无关召回、I14 注入预算、F03 topic 复用 / 聚合 | 直接决定记忆效果，进入 S1-S5 |
| 当前必要小修 | I01 共享边删除、I02 零权图路 | 已复现且当前实例有暴露面；S0 小范围修复，不借机扩建框架 |
| 随路护栏 | I03 / I04 来源幂等、I06 重建等价 | 只有在 S1 改总结提交、S3 改图重建时一并处理到“不制造新问题”的程度 |
| 后置治理 | I07 Consolidation、I13 access 语义、F01-F03 | 不阻塞主线；等写入、图和召回模型稳定后再决定 |
| 观察 / 低优先 | I12 event-only、U01 / U02 展示与格式 | 有真实代码依据，但不为它们单开阶段 |

“必要小修”并不意味着进行普遍性的防御编程。只修已经复现、且会干扰当前主线或当前运行状态的具体行为。

| 顺序 | ID | 问题 | 类型 | 级别 | 证据 | 当前暴露 | 核心方案 | 验收摘要 | 状态 |
|---:|---|---|---|---|---|---|---|---|---|
| 1 | I01 | 共享图边单一所有权导致删除一条记忆时抹掉其他记忆的图 entry | FIX | P0 | E1 + E2 | 当前库有 56 个跨来源 entry / 3 条共享边 | edge 与 memory evidence 解耦；删除只删该 memory 的 evidence / entry；无引用才删 edge | 两条记忆共享边，删除任意一条后另一条仍可图召回；迁移审计无悬挂引用 | Ready |
| 2 | I02 | 图权重 0 仍执行图路且零分候选可占位 | FIX | P1 | E1 + E4 | 当前正是 graph enabled + weight 0 | 权重 / 路线关闭时不调图；融合后拒绝无贡献候选 | mock 断言图查询未调用；结果无 0 分图独有项；延迟回到 no-graph 量级 | Ready |
| 3 | I03 | 自动总结提交缺少幂等键 | FIX | P1 | E3 | 当前无精确重复，但存在崩溃窗口 | 以 scope + 稳定消息 ID 范围 / fingerprint 建唯一提交键；游标与提交可恢复 | 注入“写成功、游标失败”故障后重试不产生第二条记忆 | Inline with S1 |
| 4 | I04 | `source_window` 使用可变 OFFSET，不是持久来源坐标 | FIX | P1 | E2 | 当前重复窗口坐标 11 组、额外 19 条；不等于正文重复 | 保存 first / last message_id、message_count、content fingerprint；旧字段仅兼容 | 裁剪消息后仍能定位原来源；旧数据迁移结果可审计 | Inline with S1 |
| 5 | I05 | 低质量或兜底解析结果仍作为 active 记忆召回 | FIX | P1 | E2 | 当前 8 条 low-quality 文档 | 建质量门与 `quarantined` / `failed_summary`；不得直接进 active 索引 | 解析失败、字段不足、低质量样本均不进入生产召回；可人工重试 | Ready |
| 6 | I06 | 自动、Agent、Consolidation、恢复、重建的 Atom / 图语义不一致 | FIX | P1 | E3 | 当前 consolidated 文档没有等价 Atom 生产链 | 定义标准 MemoryRecord IR；所有入口生成或明确声明同一派生物 | 同一 fixture 经各入口 / 重建产生等价 Atom 与图签名 | Inline with S3 |
| 7 | I07 | Consolidation 分组无上限且替换过程非原子 | FIX | P1 | E2 | 当前每日启用；已出现一次 7→1 合并 | 限制 group size；分批；引入 preparing / committed / superseded 状态与恢复协议 | 大 session 不会生成超大 prompt；任一步失败可恢复且不出现双 active | Deferred to S6 |
| 8 | I08 | Atom 生命周期与实际父文档召回脱节，Retriever / reinforcement 是死链 | FIX | P1 | E2 | 当前 56 个非 active Atom，但父文档可继续召回 | 先作架构决策：Atom 成为权威过滤 / 召回单位，或默认停用未消费机制 | 能回答“Atom 过期后用户是否还能召回该事实”，且自动、工具、维护行为一致 | Decision needed |
| 9 | I09 | 事实与实体绑定过粗，图关系持续膨胀 | OPT-S | P2 | E2 | 77 文档产生 2504 entries，平均 35.77 / 文档 | 输出 fact-level entity links；只为有证据的关系建边；重建一致 | 人工标注集上错误边率下降；entry / doc 可控；不损失关键关系 | Planned |
| 10 | I10 | 最近记忆硬占槽、绕过统一相关性排序 | OPT-S | P2 | E2 + E4 | 当前 4 个槽最多 2 个硬保留 | recent 作为特征 / 候选源进入统一重排；仍须过相关性门 | 无关查询不因“最近”被强塞；近期相关项仍获可解释加分 | Planned |
| 11 | I11 | 缺少 abstain / 空结果契约 | OPT-S | P2 | E4 + E3 | 离线 4 个负向查询均返回 8 条 | 引入绝对阈值、margin / calibration 与空结果；按 route 分析 | 负向集显著减少注入且正向 Hit@k 不出现不可接受下降 | Planned |
| 12 | I12 | `event_only` 允许无 `atom_types` 的未知记录通过 | FIX | P2 | E3 | Agent memorize / legacy 记录可能没有类型 | 未知类型默认不满足 event-only，或明确配置 unknown policy | event-only 测试中无类型文档不再静默通过 | Observe |
| 13 | I13 | 逐路最大归一化不可校准，访问计数把“返回”当“使用” | OPT-S | P2 | E3 | 所有自动 / 工具搜索结果都会强化 | 保存 raw score 与 route；离线标定；分离 retrieved / injected / adopted | 排名阈值跨查询可解释；衰减不再被未使用候选污染 | Deferred |
| 14 | I14 | 注入无预算，父文档和字段存在重复 | OPT-S | P2 | E3 | top_k / 长正文扩大时直接占上下文 | token-aware budget；facts 优先；摘要去重；单条与总量双上限 | 极端长记忆下不超预算；关键信息保留；格式稳定 | Planned |
| 15 | F01 | 矛盾检测、SUPERSEDED 与新旧事实替代没有闭环 | FEAT | 后置 | E2 | 当前 0 superseded | 写入时召回候选 + 离线周期扫描；高置信才替代，保留证据与可撤销状态 | 标注矛盾集上准确；不会因主题相似误替代；可回滚 | Deferred |
| 16 | F02 | Atom 直接召回 / 事实级注入尚未形成能力 | FEAT | 后置 | E2 | AtomRetriever 无生产调用 | 仅在 I08 决策选择 Atom 权威后接入双阶段检索 | 事实级命中提升且 parent 拼装、TTL、scope、权限一致 | Blocked by I08 |
| 17 | F03 | topic 复用、alias 与聚合能力有限 | OPT-S | P2 | E3 | 当前只做文本规范化 | 在写入时优先复用已有 topic，并在图正确后增加可解释、可撤销的 alias 合并 | 固定总结集不持续制造近义 topic；误合并率受控 | Planned in S1 / S3 |
| 18 | U01 | WebUI 缺少路线是否真实执行、质量隔离、修复队列等观测 | UX | P3 | E3 | 运维需查日志 / DB | 展示 route bypass、候选淘汰原因、质量状态、write-op、consolidation 状态 | 不查数据库即可回答一条记忆为何写入 / 召回 / 被过滤 | Deferred |
| 19 | U02 | 注入格式与语言可继续精简 | UX | P3 | E2 | 现行自定义 header 已显著缓解复述 | 基于预算器减少标签噪声；保持“当前对话优先” | A/B 不增加复述率，减少 token，用户体验不退化 | Deferred |

---

## 7. 精炼后的实施主线

截图中的八个问题可以压缩成一条连续的效果链：

```text
总结产物失控
  → topic / fact 关系失真
  → 图谱和召回候选失真
  → 注入后表现为无关回忆、硬塞最近记忆、复述自己的记忆
```

Atom 不是第五条平行主线，而是必须在中间决定“要不要成为事实层”的结构选择。

### 7.1 聊天问题映射

| 聊天中的观察 | 调查后的精确表述 | 所属步骤 |
|---|---|---|
| topics 过于碎片化，每个 topic 与每个 fact 都连边 | topic 命名自由漂移；Atom 又把整组 topics / participants 复制给每条 fact，形成近似笛卡尔关系 | S1、S3 |
| 希望相近 topic 真正聚合，并能形成联想 | 当前 resolver 主要做文本规范化，没有可靠的 topic alias / 聚类；图边本身又缺少事实级证据 | S3 |
| 总结写入没有控制，fact / topic 想到什么写什么 | 解析有格式要求，但没有受控词表、候选复用和有效质量门 | S1 |
| 原子衰减没用 | Atom 状态会变化，但父文档照常参加生产召回 | S2、S6 |
| Atom 分类粗，很多 Unknown，且没参与回想 | 分类是规则推断；AtomRetriever 没有生产调用者 | S2 |
| 召回预算曾经效果不好而撤回 | 不能只做粗暴截断；应在候选质量稳定后做 token-aware 装配 | S4 |
| top_k 中硬塞最近记忆，另外候选受图噪声影响 | 当前实例 top_k=4、recent=2；recent 预留槽位，图权重 0 仍执行 | S0、S4 |
| bot 喜欢炫耀 / 复述自己的记忆 | 当前自定义 header 已缓解；剩余问题需要区分“候选不相关”和“表达方式生硬” | S5 |

另外，召回并不是“纯向量相关性”：实际是文档 BM25 + 向量，以及图关键词 + 图向量，分别融合后再做跨路融合。不能只调一个相似度阈值。

### S0：两个必要小修 + 最小效果基线

| 项目 | 内容 |
|---|---|
| 修复 1 | 共享图边删除不能连带抹掉其他记忆的引用 |
| 修复 2 | 图权重为 0 时真正跳过图检索，零分图候选不能占位 |
| 基线 | 保留现有正向 / 负向查询样本，增加 topic 碎片、图错连、recent 硬塞、复述四类小样本 |
| 范围限制 | 不建设通用事务框架、全量可观察平台或复杂故障注入体系 |
| 退出条件 | 两个已复现缺陷转为回归测试；能够重复比较后续每步对记忆效果的影响 |

I01 很重要，但重要性来自一个明确错误：当前每日 Consolidation 会归档旧记忆，而归档会走图删除。修复应保持窄范围，不扩张为全面存储重构。

### S1：控制总结产物

| 项目 | 内容 |
|---|---|
| 目标 | 让写入端产出稳定、可复用、可建图的事实，而不是事后清理随机词汇 |
| topics | 总结时提供同 scope 的已有 topic 候选，优先复用；只有确有新概念时创建新 topic |
| facts | 每条 fact 独立、可判断，避免把多件事塞在一句；保留必要时间与主体 |
| 关系 | 输出 fact 对应的 topics / participants，而不是给所有 fact 复制整组实体 |
| 质量 | 缺主体、空泛、解析兜底和明显低质量结果不直接进入 active 召回 |
| 随路护栏 | 改总结提交时，使用稳定 message ID / fingerprint 防止同一窗口重复写；不另开“幂等工程” |
| 验证 | 固定对话集重复总结时 topic 数量不持续发散，fact-topic 人工抽查准确率提高 |

### S2：决定 Atom 到底是什么

| 项目 | 内容 |
|---|---|
| 必答问题 | Atom 是生产召回的事实单元，还是只用于辅助建图的派生结构？ |
| 如果采用 | Atom 必须承接 S1 的事实级关系，并真实参与召回 / 父文档装配；类型 Unknown 只在确实无法判断时出现 |
| 如果不采用 | 停止独立 Retriever、FTS、TTL 和 lifecycle 的无效成本；不继续维护一套用户不可见的状态机 |
| 暂不做 | 不在此时实现矛盾检测、SUPERSEDED、复杂 reinforcement |
| 验证 | 能用一句明确规则回答“Atom 过期后，用户还能否召回这条事实”，且代码行为一致 |

### S3：重建有意义的图谱

| 项目 | 内容 |
|---|---|
| 建边 | 只根据 S1 的显式 fact-entity / fact-topic 关系建边，移除全组合逻辑 |
| topic 聚合 | 先做可解释的规范化与 alias 候选；相似 topic 的合并必须能查看和撤销，不直接用一次向量近邻永久合并 |
| 联想 | 图召回应能说明“当前查询 → 哪个节点 → 哪条事实 → 哪条记忆”，而不是仅靠共享宽泛 topic |
| 数据正确性 | 完成共享 edge ownership 修复，并让在线写入与 rebuild 使用同一派生逻辑 |
| 验证 | 错边率、entries / memory、图独有有效命中、重建前后结构一致性 |

### S4：整理召回、排序与预算

| 项目 | 内容 |
|---|---|
| 路线 | 文档 BM25 / 向量与图路线各自保留 raw score 和来源；关闭路线就不执行 |
| 融合 | 先用 S0 数据比较路线贡献，再决定 RRF / 权重；不预设图一定有益 |
| recent | 不再硬占两个槽；作为时效特征进入同一候选池，仍需与查询相关 |
| 无关召回 | 允许空结果；用负向集确定最低门槛，而不是永远凑满 top_k |
| 预算 | 排名稳定后再加 token-aware 预算；优先保留命中 fact，去掉 summary / facts 重复 |
| 验证 | 正向命中、负向注入率、延迟、注入 token、recent 相关性共同评估 |

### S5：解决“记得自然”，不是继续堆提示词

| 项目 | 内容 |
|---|---|
| 先分因 | 如果记忆本身无关，修 S4；如果相关但回复像播报数据库，才是 S5 |
| 当前基础 | 保留现行“不要宣布 / 复述记忆、当前对话优先”的自定义 header |
| 注入形态 | 注入最少的相关事实，不把整条记录、所有标签和元数据都交给模型炫耀 |
| 人格表现 | 用行为样本判断是否自然融入回答，而不是把“我记得……”一律当成失败 |
| 验证 | 同一组相关 / 无关 / 模糊查询做盲测，比较复述率、自然度和事实使用正确率 |

### S6：主线稳定后的生命周期和新能力

| 项目 | 内容 |
|---|---|
| Atom 生命周期 | 只有 S2 选择 Atom 为事实层时，才继续设计 TTL、reinforcement 和遗忘对召回的影响 |
| Consolidation | 根据 S1-S3 的新结构重新决定合并单位；组大小 / 非原子问题届时一并处理 |
| 矛盾 | 写入时候选检测、周期扫描、SUPERSEDED 属于新能力，放在事实层稳定之后 |
| topic 语义聚类 | 可以逐步增强，但必须建立在可解释 alias 与误合并评估上 |
| UX / WebUI | 最后补实际路线、淘汰原因和记忆来源展示，不阻塞前面效果验证 |

### 7.2 主线依赖

```text
S0 两个小修 + 效果基线
  ↓
S1 控制总结产物
  ↓
S2 决定 Atom 的角色
  ↓
S3 重建图关系与 topic 聚合
  ↓
S4 召回、排序、recent 与预算
  ↓
S5 记忆的自然使用
  ↓
S6 生命周期、矛盾等后续能力
```

这不是要求每一步都做成大版本。S0 应当很小；S1-S5 每步先形成可验证的行为变化，再决定是否继续细分。

---

## 附录 A：完整风险展开（不作为实施顺序）

以下 R0-R12 是调查收口时的完整风险拆解，保留用于追溯，但已由 S0-S6 取代为实施主线。

### 原分步骤改造路线图

路线不是固定的 T1-T5。阶段数量由依赖关系展开；每一步都必须单独改造、单独验证、单独决定是否进入下一步。

### R0：建立不可漂移的回归基线

| 项目 | 内容 |
|---|---|
| 类型 | 改造前置，不新增生产能力 |
| 目标 | 把本次定向复现、代表性写入与召回样本固化为自动测试 / fixture |
| 包含 | 共享边删除、零权图路、总结幂等故障点、多入口派生一致性、负向查询、Consolidation 故障注入 |
| 产物 | 固定基线测试集；基线指标；数据库 schema 快照；可重复迁移演练 |
| 退出条件 | 测试能在无运行数据副作用的隔离环境稳定复现 I01、I02，并记录其余风险的当前行为 |

### R1：修复图共享边所有权（I01）

| 项目 | 内容 |
|---|---|
| 类型 / 级别 | FIX / P0 |
| 先做原因 | 后续任何归档、删除、Consolidation、重建都会依赖图删除正确性 |
| 改造 | 把语义 edge 与来源 evidence 建成多对多；entry / evidence 按 memory 独立删除；最后一个引用消失后再回收 edge |
| 数据迁移 | 先备份；从现有 entries 重建 ownership；对 56 个跨来源链接重点核验；保留审计报告 |
| 验证 | 双来源 / 多来源共享边删除、批删、归档、恢复、Consolidation、write-op replay 全覆盖 |
| 暂停条件 | 不能无歧义恢复历史 edge 来源时，不自动删边，转人工审计集合 |

### R2：让路线开关语义真实（I02）

| 项目 | 内容 |
|---|---|
| 类型 / 级别 | FIX / P1 |
| 改造 | `enabled=false` 或 route weight ≤ 0 时不构造 / 不调用该路线；跨路融合拒绝无贡献候选 |
| 配置契约 | 区分 `graph_enabled`、`graph_retrieval_enabled` 与 weight；UI / 文档明确含义 |
| 验证 | 调用计数为 0、结果无 0 分占位、延迟对比、打开路线时行为不退化 |

### R3：写入幂等与稳定 provenance（I03、I04）

| 项目 | 内容 |
|---|---|
| 类型 / 级别 | FIX / P1 |
| 改造 | 使用稳定 message IDs；生成 source fingerprint / submission key；数据库唯一约束；游标推进变为可重放提交步骤 |
| 兼容 | 保留旧 start / end index 只作展示；迁移时标注 provenance 可信等级 |
| 验证 | 进程在文档写入、来源写入、游标推进前后任一点退出，重启后都只能得到一份 active 记忆 |

### R4：总结质量门与失败隔离（I05）

| 项目 | 内容 |
|---|---|
| 类型 / 级别 | FIX / P1 |
| 改造 | 结构校验、内容最低要求、重复 / 空泛检测；失败写 `quarantined` 记录或任务，不进入索引 |
| 重试 | 区分 provider 失败、解析失败、质量失败；保留原输入指纹，重试仍服从幂等键 |
| 验证 | 低质量、正则兜底、空事实、超长、群聊混淆样本；现有 8 条只审计不擅自改写 |

### R5：统一所有写入、恢复与重建语义（I06、I12）

| 项目 | 内容 |
|---|---|
| 类型 / 级别 | FIX / P1-P2 |
| 改造 | 定义唯一 MemoryRecord IR；正文、metadata、Atom、图派生都由它生成；Agent / 导入 / Consolidation 明确填充规则 |
| 重建 | rebuild 必须从可持久 IR 得到与在线写入等价的 Atom / 图；不能退回另一套笛卡尔逻辑 |
| 顺带修复 | event-only 的 unknown policy 显式化 |
| 验证 | 同 fixture 经自动、手动、Agent、Consolidation、restore、rebuild 后规范化签名一致 |

### R6：Consolidation 安全化（I07）

| 项目 | 内容 |
|---|---|
| 类型 / 级别 | FIX / P1 |
| 改造 | group memory count / token 双上限；大组分批；替换状态机；来源链；失败恢复；先验证新记忆完整再提交旧状态 |
| 与 I01 关系 | 必须在图 ownership 修好后再启用新删除 / 归档流程 |
| 验证 | 超大 session、语义链式连通、LLM 失败、索引失败、归档失败、重启 replay |

### R7：Atom 架构决策与生命周期闭环（I08）

| 项目 | 内容 |
|---|---|
| 类型 / 级别 | FIX / P1 架构决策 |
| 方案 A | Atom 是权威事实单元：召回、TTL、reinforcement、SUPERSEDED 与父文档组装全部接通 |
| 方案 B | 文档是唯一权威：Atom 只作图构建派生物，默认不跑独立生命周期 / FTS / Retriever |
| 禁止状态 | 继续同时付出 Atom 存储和生命周期成本，却让召回完全忽略其状态 |
| 决策指标 | 事实级命中、错误过期率、写放大、查询延迟、可解释性、迁移成本 |
| 退出条件 | 选定一个权威模型，并用端到端测试证明状态变化会产生一致的用户可见行为 |

### R8：图关系降噪与证据化（I09）

| 项目 | 内容 |
|---|---|
| 类型 / 级别 | OPT-S / P2 |
| 改造 | 每条 fact 明确绑定 participants / topics；边保存来源证据、置信度与抽取版本；弱关系不默认建边 |
| 指标 | entries / memory、错误边率、图路 Hit@k、跨记忆共享边精度、重建一致性 |
| 前置 | R1 ownership、R5 IR、R7 Atom 边界均稳定 |

### R9：召回校准、最近记忆重排与空结果（I10、I11、I13）

| 项目 | 内容 |
|---|---|
| 类型 / 级别 | OPT-S / P2 |
| 改造 | 保留 raw scores；离线标定；recent 作为统一特征；绝对阈值 / margin；允许空结果；区分 retrieved / injected / adopted |
| 验证集 | 正向、负向、时效性、同主题不同事实、跨 session、图独有命中 |
| 门槛 | 先定义可接受的正向损失和负向注入下降目标，再选算法，不反向迎合单一旧数据集 |

### R10：注入预算与事实级装配（I14）

| 项目 | 内容 |
|---|---|
| 类型 / 级别 | OPT-S / P2 |
| 改造 | 单条 / 总 token 预算；命中事实优先；摘要与 facts 去重；必要时只带最小 provenance |
| 前置 | R7 确定是否支持事实级命中，R9 给出最终候选与置信度 |
| 验证 | 不同上下文窗、长记忆、多候选、中英混合；保证不截断关键语义 |

### R11：矛盾与 SUPERSEDED（F01、F02、F03）

| 项目 | 内容 |
|---|---|
| 类型 | FEAT |
| 顺序 | 所有关键 FIX 与严重优化通过后再启动 |
| 第一阶段 | 写入时只在同 scope 召回有限候选，判定支持 / 冲突 / 无关；不直接删除旧事实 |
| 第二阶段 | 离线批量扫描补漏；建立 evidence、confidence、review / rollback |
| 状态 | 仅在高置信且有明确新旧关系时写 SUPERSEDED；含时效或条件差异时并存 |
| 可选后续 | 若 R7 选择 Atom 权威，再接 AtomRetriever 和语义 alias；否则不新增第二套死链 |

### R12：可观察性与 UX 收尾（U01、U02）

| 项目 | 内容 |
|---|---|
| 类型 | UX / P3 |
| WebUI | 展示实际执行路线、raw / final score、过滤原因、质量状态、来源、Consolidation / repair 状态 |
| 注入格式 | 在预算内精简重复标签；保留现行“不复述、当前对话优先”约束 |
| 文档 | 配置名与真实运行语义一致；明确默认值和迁移影响 |

---

### A.1 原阶段依赖图

```text
R0 回归基线
 ├─ R1 图所有权 P0 ───────────────┐
 ├─ R2 真正关闭零权路线          │
 └─ R3 幂等 + 稳定来源 ─ R4 质量门│
                                  ↓
                       R5 统一写入 / 重建 IR
                          ├─ R6 Consolidation 安全化
                          └─ R7 Atom 权威边界决策
                               ├─ R8 图关系降噪
                               └─ R9 召回校准 / 空结果
                                      ↓
                                  R10 注入预算
                                      ↓
                                  R11 新功能
                                      ↓
                                  R12 UX
```

允许并行的只有互不修改同一契约的验证工作。涉及 graph schema、MemoryRecord IR、Atom 权威边界的阶段不得并行落生产代码。

---

### A.2 原完整交付契约

后续每一个 R 阶段都必须填以下表，而不是只写“完成”：

| 字段 | 必填内容 |
|---|---|
| 基线 | 开始时 commit、配置、schema 版本 |
| 问题 | 对应 I / F / U 编号和真实性证据 |
| 行为契约 | 改造前、改造后、明确不改什么 |
| 数据影响 | schema、迁移、回滚、备份、兼容窗口 |
| 生产路径 | 自动、工具、手动、后台、恢复、重建分别是否受影响 |
| 测试 | 单元、集成、故障注入、迁移、回归、性能 |
| 观测 | 新增日志 / 指标，如何判断线上行为符合预期 |
| 验收 | 可机器验证的通过条件 |
| 结果 | Done / Partial / Blocked；未完成项不得藏入下一阶段 |

阶段状态只使用：

```text
Planned → Ready → In Progress → Verified → Done
                     └────────→ Blocked
```

---

## 10. 本轮验证记录

| 验证 | 结果 | 解释 |
|---|---|---|
| Git 基线 | clean `master`，跟踪 `upstream/master`，commit 固定为 `c2e7330…` | 本轮结论未随上游漂移 |
| 后端测试 | 743 passed，4 warnings，41.25s | 使用实际 AstrBot Python，测试依赖放在临时目录；仓库未写入 |
| 前端测试 | 15 passed，0 failed | WebUI 测试通过 |
| 共享边删除定向复现 | confirmed | 删除来源 1 后来源 2 的共享 edge entry 同时消失 |
| 零权图路定向复现 | confirmed | 返回文档结果 1.0 与图独有结果 0.0；图路没有旁路 |
| 运行库只读检查 | completed | 无 pending repair、无精确重复正文、无当前孤立 Atom；同时确认共享边暴露面和低质量文档 |

测试通过只能说明现有断言成立。I01 的共享边删除场景此前没有测试覆盖；I02 也缺少“权重为零时不调用路线”的行为契约测试。

---

## 11. 关键结论证据索引

此表把问题登记表映射回固定基线的实际文件与函数。行号只对本文顶部固定 commit 有效；后续代码移动时保留函数名和提交证据。

| 问题 | 写入 / 读取位置 | 调用者与消费者 | 已有测试 / 本轮补充证据 |
|---|---|---|---|
| I01 共享边删除 | `storage/graph_store_write.py:259-285` 语义边复用；`:565-609` 按单一来源删边；`core/managers/graph_memory_manager.py:118-140` 删除入口 | `MemoryEngine.delete_memory`、batch delete、archive、Consolidation | 现有 `tests/test_graph_memory.py` 覆盖合并但未覆盖跨来源删除；本轮临时库定向复现确认 |
| I02 零权图路 | `core/retrieval/dual_route_retriever.py:44-167` 双路执行、归一化与融合；`core/managers/memory_engine_crud.py:425-451` 生产调用 | 自动召回、Agent search tool 都经 `MemoryEngine.search_memories` | 本轮 stub route 定向复现；2026-08-19 eval 的 zero-graph 延迟与代码事实一致 |
| I03 总结幂等 | `core/event_handler_modules/memory_reflection.py:291-322` in-flight；`:329-493` 写记忆后推进游标；`core/managers/memory_engine_crud.py:19-247` 多存储写入 | `main.py:346-358` 最终响应 hook | 现有测试未覆盖“add 成功、游标失败、重启重试”的精确故障点 |
| I04 来源坐标 | `core/event_handler_modules/memory_reflection.py:408-418` 写 source_window；`core/managers/conversation_manager.py:627-705` OFFSET 读取；`storage/conversation_store_messages.py:200-305` 裁剪并回退索引 | 总结器、手动 summarize、来源展示 | 当前库只读统计发现坐标复用；按正文 + scope 未发现精确重复，故未误判为现成重复写 |
| I05 质量门 | `core/processors/memory_processor.py:362-466` 总结与质量标签；`core/processors/memory_processor_parse.py` JSON 修复 / 正则兜底；`memory_processor_build.py:31-70` 文档构建 | 自动 / 手动总结后直接进入 `add_memory` | `tests/test_memory_processor.py` 覆盖解析行为；当前库有 8 条 low-quality active 文档 |
| I06 入口 / 重建不等价 | `core/processors/graph_extractor.py:21-299` legacy；`:300-435` Atom 路；`core/tools/memory_memorize_tool.py:100-177` 无 Atom；`core/managers/memory_engine_batch.py:287-364` restore 重分类；`memory_engine_crud.py:814+` rebuild 只给文档 | Agent memorize、Consolidation、restore、graph rebuild | `tests/test_memory_engine.py`、`tests/test_graph_memory.py` 有局部测试；缺少跨入口规范化等价测试 |
| I07 Consolidation | `core/managers/consolidation_manager.py:42-95` 执行；`:98-203` 候选和分组；`:205-250` 先写新再处理旧 | `core/schedulers/decay_scheduler.py` 每日调度；reflection 可触发 | `tests/test_consolidation_manager.py` 有功能测试；当前实例已发生一次 7→1，未用于证明失败已发生 |
| I08 Atom 死链 | `core/managers/memory_engine.py:195-207` 构造 lifecycle / Retriever；`core/managers/atom_lifecycle_manager.py:36-137` 状态与 reinforcement；`core/retrieval/atom_retriever.py:30+` 检索器 | 代码搜索未发现 AtomRetriever 和 reinforcement 的生产调用；文档召回仍走 `search_memories` | `tests/test_memory_atom.py` 证明局部机制，不证明生产可达；当前库 24 expired、32 forgotten、0 superseded |
| I09 图过连 | `core/processors/atom_classifier.py:130-181` 每 Atom 接收整组实体；`core/processors/graph_extractor.py:339-435` fact 对 entities 建边；legacy `:217-261` 组合建边 | 所有 Atom 建图与 legacy 建图入口 | 当前库平均 35.77 entries / memory；该数只说明膨胀面，关系错误率仍需标注集 |
| I10 recent 硬占位 | `core/managers/memory_engine_write_ops.py:219-299` recent 查询、1.0 分与预留槽；`memory_engine_crud.py:441-446` 合并调用 | 自动与工具召回 | 现有测试未形成“recent 也必须相关”的契约；当前 top_k=4、recent=2 |
| I11 无 abstain | `core/managers/memory_engine_crud.py:380-451` 搜索返回；`memory_engine_write_ops.py:159-217` 后过滤 | 格式器只消费收到的候选，无独立拒答层 | 2026-08-19 的 4 个负向样本在各变体均得到 8 个结果；仅为小样本证据 |
| I12 event-only unknown | `core/managers/memory_engine_write_ops.py:182-214` 仅在 atom_types 为非空 list 时排除非事件 | 自动与工具召回共用过滤器 | 代码条件可直接确认；需补 unknown 类型用例 |
| I13 score / access 语义 | `core/retrieval/dual_route_retriever.py:56-167` 逐路归一；`memory_engine_crud.py:406,451,992-1037` 返回后即更新 access | 衰减逻辑后续读取 access_count | 当前测试主要覆盖数值与更新，不覆盖 injected / adopted 语义 |
| I14 注入预算 | `core/event_handler_modules/memory_recall.py:240-326` 搜索、格式化与注入；`core/utils/formatting.py:69-188` 输出字段 | ProviderRequest 的 `extra_user_content_parts`，由当前 LLM 消费 | 当前无总 token / 字符预算断言；现行自定义 header 已在运行配置中核实 |
| F01 SUPERSEDED | `core/models/memory_atom.py:30` 枚举；全仓生产代码无状态写入者 | 无实际消费者 | 当前库 superseded=0；这是待建能力，不冒充已有 FIX |

### 11.1 生产可达性索引

| 路径 | 注册 / 启动 | 真实消费者 | 当前结论 |
|---|---|---|---|
| 自动总结 | `main.py:346-358` → `memory_reflection.py:79+` | `MemoryProcessor` → `MemoryEngine.add_memory` | 可达 |
| 群聊被动捕获 | `main.py:325-330` → `group_capture.py:42-95` | ConversationStore → 后续总结 | 可达 |
| 自动召回 / 注入 | `main.py:333-343` → `memory_recall.py:87-326` | 当前 ProviderRequest / LLM | 可达 |
| Agent search / memorize | 工具注册与配置启用后由 Agent 调用 | `search_memories` / `add_memory` | 当前启用；memorize 与主写入派生不等价 |
| AtomRetriever | `memory_engine.py:206` 只构造 | 无生产调用者 | 不可达 |
| Atom lifecycle | `memory_engine.py:195-207` 启动 | AtomStore 状态维护 | 可达，但不控制文档召回 |
| Consolidation | `plugin_initializer_finalize.py:259-310` 构造并交给调度器；reflection 可触发 | 生成新文档并归档 / 删除旧文档 | 当前每日启用 |
| 图重建 / 恢复 | 管理命令、维护与 restore | 图 / Atom 派生存储 | 可达，但与在线语义不完全等价 |

---

## 12. 冻结结论与下一动作

1. T0-A、T0-B 已完成，完整系统地图已经建立；不再停留在“继续调查后再决定阶段”的状态。
2. 后续不沿用旧 T1-T5，也不限制必须五步完成；实施主线精炼为 S0-S6。R0-R12 仅作为完整风险附录保留。
3. 开工顺序冻结为：`FIX → OPT-S → FEAT → UX`。
4. 第一个代码阶段不是矛盾检测或全面基础设施改造，而是 S0：两个已复现小修和一组最小效果基线。
5. 任何当前运行数据迁移前，必须先做可恢复备份与离线迁移演练；本轮没有执行迁移。
6. Excel 不再更新。路线、状态、验收和新增证据都回填本文。

### 下一步入口

```text
S0：修共享边删除与零权图路，并固定效果样本
  ↓
S1：控制总结输出、topic 复用和 fact 关系
  ↓
逐步验证 S2-S5，不把后置治理提前塞入主线
```
