> [!IMPORTANT]
> **这是 LivingMemory 的增强 fork。** 本项目由 [2718labs](https://github.com/2718labs) 基于[原版 LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)继续开发，重点改进记忆的拆分、事实召回、跨轮连续性与真实注入边界。原版已有能力仍保留；fork 的实际变更见 [CHANGELOG_FORK.md](CHANGELOG_FORK.md)，后续设想见 [S0–Sfuture 路线图](docs/livingmemory-roadmap/README.md)。

<div align="center">

<p><strong>中文</strong> &nbsp;/&nbsp; <a href="README_en.md">English</a> &nbsp;/&nbsp; <a href="README_ru.md">Русский</a></p>

<h1>LivingMemory</h1>

<p><strong>为 AstrBot 构建的长期记忆：精准召回，并在每次对话中持续演化。</strong></p>

<p><sub>捕获 &nbsp;&nbsp; 检索 &nbsp;&nbsp; 连接 &nbsp;&nbsp; 演化</sub></p>

<p>
  <a href="https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/releases"><img src="https://img.shields.io/github/v/release/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory?style=flat-square&color=5f7f79" alt="最新版本"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-e9f1ef?style=flat-square&labelColor=263a36" alt="Python 3.10 或更高版本">
  <img src="https://img.shields.io/badge/AstrBot-%3E%3D%204.24.2-f3eee4?style=flat-square&labelColor=544c3d" alt="AstrBot 4.24.2 或更高版本">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-f2e8e5?style=flat-square&labelColor=5b403a" alt="AGPL-3.0 许可证"></a>
</p>

<img src="docs/public/images/retrieval-flow.svg" width="100%" alt="LivingMemory 双路检索流程">

</div>

## 这个 fork 额外做了什么

<table>
<tr>
<td width="33%"><strong>按事实召回</strong><br><br>父记忆保存一段对话的整体概览，具体事实各自参与检索、排序与注入。命中一条事实时，不再把同一父记忆中的无关内容一起带回。</td>
<td width="33%"><strong>一窗口一父记忆</strong><br><br>每个总结窗口只产生 0 或 1 条父记忆，最多容纳 5 条事实。父层按连续对话组织，事实层再承担语义拆分，避免同一窗口被重复切成多个语义岛。</td>
<td width="33%"><strong>跨轮证据连续性</strong><br><br>采用固定 <code>1+2+X</code> 滑窗：本轮最多召回 <code>X=top_k</code> 条，上轮续带 2 条，上上轮续带 1 条；事实再次命中时会刷新生命周期。</td>
</tr>
<tr>
<td width="33%"><strong>近期记忆补位</strong><br><br>RecentBlock 可从指定的第几个父记忆开始，补充刚滑出原始上下文的概览与相关事实，并与主召回共用同一 token 硬预算。该功能默认关闭，适合按实际上下文长度手动启用。</td>
<td width="33%"><strong>摘要与事实分工</strong><br><br>总结模型顺手生成覆盖整条父记忆的中性概览，WebUI 与 RecentBlock 读取这份概览；可核验、可召回的证据仍以 canonical facts 为准。</td>
<td width="33%"><strong>可诊断的注入链路</strong><br><br>对话召回日志按查询、本轮召回、候选装配和注入结果分阶段展示，能够看见 <code>top_k</code> 占用、滑窗来源、去重、预算和最终注入方式。</td>
</tr>
</table>

Topic 会优先复用同一作用域内的已有名称，减少近义标签反复新增。图谱数据结构继续保留，但图路召回、RecentBlock 与重要性宽容准入默认关闭；事实召回与跨轮滑窗是当前生产主线。

## 让记忆形成结构

<table>
<tr>
<td width="33%"><strong>精准召回</strong><br><br>BM25 与向量检索同时覆盖文档路和图路，最终通过融合排序收敛为可靠结果。</td>
<td width="33%"><strong>动态上下文</strong><br><br>事实被拆分为独立记忆原子，分别拥有重要度、TTL、强化机制与时间衰减。</td>
<td width="33%"><strong>全量可见</strong><br><br>通过社区结构与多级细节，在高性能画布中探索完整的记忆关系图谱。</td>
</tr>
</table>

## 一套完整的记忆系统

| 召回 | 智能 | 控制 |
| :--- | :--- | :--- |
| **混合检索**<br>关键词与语义检索覆盖两条数据路径。 | **双通道总结**<br>事实信息与人格上下文保持独立价值。 | **安全操作**<br>自动备份、事务删除与重建失败回滚。 |
| **Agent 原生工具**<br>`recall_long_term_memory` 与 `memorize_long_term_memory`。 | **时间感知图谱**<br>关系置信度随证据累积或消退动态变化。 | **专注的管理界面**<br>管理记忆、调试召回并检查完整图谱。 |

## 近期能力

| 可恢复记忆 | 可控边界 | 在线维护 |
| :--- | :--- | :--- |
| **原文与归档**<br>重要记忆可保留来源消息，支持核验、重新总结；低价值记忆可归档并恢复，而非直接删除。 | **作用域与访问控制**<br>可按会话、用户或全局共享记忆，并通过强制隔离、白名单和身份别名明确边界。 | **安全索引重建**<br>启动检查和大规模修复在后台运行，使用分批重建、进度状态、失败回滚和影子索引切换。 |

```mermaid
flowchart LR
    A[对话] --> B[总结]
    B --> C[原子化与索引]
    C --> D[混合召回]
    D --> E[强化]
    C --> F[衰减或过期]
    E --> C
```

## 三步开始

1. 从 AstrBot 插件市场安装，或将插件放入 `data/plugins`。
2. 重载 AstrBot，进入 LivingMemory 配置页面。
3. 选择下方两个 Provider；其余配置均提供实用默认值。

| 配置项 | 用途 |
| :--- | :--- |
| `embedding_provider_id` | 嵌入模型；留空则使用 AstrBot 默认配置。 |
| `llm_provider_id` | 总结模型；留空则使用 AstrBot 默认配置。 |

可视化工作区入口为 `插件 -> LivingMemory -> Pages -> dashboard`。插件 Pages 需要 **AstrBot 4.24.2 或更高版本**。

## 深入了解

| 入门 | 配置 | 使用 | 原理 |
| :--- | :--- | :--- | :--- |
| [快速开始](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/guide/getting-started)<br>[功能全览](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/features) | [完整配置](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/configuration) | [命令列表](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/commands)<br>[WebUI 管理](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/webui) | [技术架构](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/architecture) |

从 v1.4.0-v1.4.2 升级？请先检查[备份、迁移与清理配置](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/configuration#备份迁移与清理)。

## 项目

[完整文档](https://lxfight-s-astrbot-plugins.github.io/astrbot_plugin_livingmemory/) · [版本发布](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/releases) · [更新记录](CHANGELOG.md) · [问题反馈](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/issues)

社区支持：[QQ 群 953245617](https://qm.qq.com/cgi-bin/qm/qr?k=WdyqoP-AOEXqGAN08lOFfVSguF2EmBeO&jump_from=webapi&authKey=tPyfv90TVYSGVhbAhsAZCcSBotJuTTLf03wnn7/lQZPUkWfoQ/J8e9nkAipkOzwh) · 口令：`lxfight`

LivingMemory 使用 [AGPL-3.0 许可证](LICENSE)发布。
