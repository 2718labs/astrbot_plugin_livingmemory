# LivingMemory Enhancement Fork 变更记录

本文件只记录 [2718labs fork](https://github.com/2718labs/astrbot_plugin_livingmemory) 相对[原版 LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)产生的变更。原项目自身的版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## [Unreleased]

### S0：逐事实准入

- 自动总结改为逐条输出候选事实及 `store / skip` 决定，程序统一推导窗口的 `store / skip / invalid` 结果。
- 增加严格 JSON 结构检查；格式失败只允许一次不改写事实的修复，仍失败时不写入、不生成兜底记忆。
- 混合窗口只把获准事实投影给现有存储和下游；全跳过时零写入并正常推进滑窗，结果无效时保留窗口进入原有重试路径。
- 自动总结与手动总结接入同一结果契约；补充中、英、俄三种手动跳过提示。
- 新增与生产顺序一致的 `top_k=4 + recent=2 + 最终注入` 基线，以及逐事实准入、格式修复和窗口状态回归测试。

### S1：v3 记忆产物契约

- 自动总结改为 `memories[]` 多产物契约：同一来源窗口可生成零条、一条或多条单中心记忆，并由自动/手动总结入口逐条写入。
- `key_facts` 升级为唯一的对象化事实真源；每条 fact 保存稳定 ID、parent 回链、自身 topic/participant、时间、重要度、来源消息和可选人格反应，不持久化第二份字符串 facts。
- `summary` 与 canonical content 由获准 facts 派生，新记录不再保存长篇 `persona_summary`。
- 增加同 scope topic 精确复用、稳定来源指纹和 active 记忆幂等写入；OFFSET 仅保留为处理过程信息，不再作为长期来源边界。
- 为旧 Atom、图谱、注入和 WebUI 消费者增加只读 fact-text 投影，避免对象 facts 被错误字符串化；完整事实索引、图关系和其他写入入口统一仍留在 S2/S3。
- 新增多中心拆分、来源时间、稳定 ID、topic 复用、fact 级实体、幂等写入和真实数据库链路回归。

## [2.6.0-a1] - 2026-08-20

### 项目

- 以原项目提交 `c2e7330` 为改造基线，使用 `refactor/enhancement` 作为 fork 的默认开发分支。
- 明确当前主线：依次改造记忆总结、处理与存储、事实与图谱、召回与注入；先处理确定的缺陷和严重结构问题，新增能力后置。
- 将 T0 调查底稿和 S0–S6 执行路线图纳入仓库 `docs/livingmemory-roadmap/`，作为当前唯一实现计划。
- 在 README 开头补充 fork 定位，并将 fork 变更与原项目更新记录分开维护。

### 变更

- 将 `graph_memory.dynamic_route_weighting` 的默认值从 `true` 改为 `false`；已有配置显式设为 `true` 时仍可启用。动态路线选择是否值得重新开启，留待主链稳定后单独评估。
