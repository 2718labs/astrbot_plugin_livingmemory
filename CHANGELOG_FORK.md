# LivingMemory Enhancement Fork 变更记录

本文件只记录 [2718labs fork](https://github.com/2718labs/astrbot_plugin_livingmemory) 相对[原版 LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)产生的变更。原项目自身的版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## [2.6.0-a1] - 2026-08-20

### 项目

- 以原项目提交 `c2e7330` 为改造基线，使用 `refactor/enhancement` 作为 fork 的默认开发分支。
- 明确当前主线：依次改造记忆总结、处理与存储、事实与图谱、召回与注入；先处理确定的缺陷和严重结构问题，新增能力后置。
- 将 T0 调查底稿和 S0–S6 执行路线图纳入仓库 `docs/livingmemory-roadmap/`，作为当前唯一实现计划。
- 在 README 开头补充 fork 定位，并将 fork 变更与原项目更新记录分开维护。

### 变更

- 将 `graph_memory.dynamic_route_weighting` 的默认值从 `true` 改为 `false`；已有配置显式设为 `true` 时仍可启用。动态路线选择是否值得重新开启，留待主链稳定后单独评估。
