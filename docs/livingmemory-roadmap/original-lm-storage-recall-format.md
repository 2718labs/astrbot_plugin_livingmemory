---
title: 原版 LivingMemory 的存储与召回格式
status: Reference
updated: 2026-08-22
baseline_commit: c2e733049392d1cfc27843fc083096a9103f27d1
---

# 原版 LivingMemory 的存储与召回格式

原版固定为分叉前的 `upstream/master@c2e7330`。下面使用原版同类的虚构姓名“张三”演示，字段和拼装方式来自原版代码。

## 1. 存储格式

一段滑窗对话只存成一篇 `documents` 文档。

### `documents.text`

```text
{第一人称人格摘要} | {fact 1}；{fact 2}；{fact 3}
```

示例：

```text
我记得张三说周三要考科目二，有点紧张，我让张三早点休息。 | 张三周三要考科目二；张三对考试有些紧张；我建议张三早点休息
```

### `documents.metadata`

```json
{
  "session_id": "aiocqhttp:FriendMessage:123456",
  "persona_id": "persona_demo",
  "importance": 0.7,
  "create_time": 1787200000.0,
  "last_access_time": 1787200000.0,
  "status": "active",

  "topics": ["驾考", "周三安排"],
  "key_facts": [
    "张三周三要考科目二",
    "张三对考试有些紧张",
    "我建议张三早点休息"
  ],
  "sentiment": "neutral",
  "interaction_type": "private_chat",

  "persona_summary": "我记得张三说周三要考科目二，有点紧张，我让张三早点休息。",
  "canonical_summary": "我记得张三说周三要考科目二，有点紧张，我让张三早点休息。 | 张三周三要考科目二；张三对考试有些紧张；我建议张三早点休息",
  "summary_schema_version": "v2",
  "summary_quality": "normal",

  "time_tags": ["2026-08-20"],
  "source_window": {
    "session_id": "原始会话 ID",
    "start_index": 0,
    "end_index": 20,
    "message_count": 20
  }
}
```

重点：`key_facts` 只是整篇文档里的字符串数组。每条 fact 没有自己的 ID、重要度、来源、状态和时间，也不能单独召回。

重要度达到默认阈值 `0.8` 时，原始消息会另外放进 `memory_sources.source_json`；它默认不参与自动召回。

## 2. 搜索返回格式

`MemoryEngine.search_memories()` 返回的每一项是整篇文档：

```python
HybridResult(
    doc_id=42,
    final_score=0.83,
    rrf_score=0.031,
    bm25_score=0.76,
    vector_score=0.81,
    content="我记得张三说周三要考科目二…… | 张三周三要考科目二；张三对考试有些紧张；我建议张三早点休息",
    metadata={...上面的整篇 metadata...},
    score_breakdown={...}
)
```

BM25、向量和图路线最终返回的都是 `documents`，不是命中的单条 fact。Atom 虽然另有表和生命周期，但 `search_memories()` 不使用 AtomRetriever。

## 3. 默认自动注入格式

默认 `extra_user_content` 最后拼给模型的是：

```text
<RAG-Faiss-Memory>
--- BEGIN HISTORICAL MEMORY REFERENCE ---
The following are historical memories extracted from past conversations.
They are provided as background reference only.
...
--- END HISTORICAL MEMORY REFERENCE ---

记忆 #1 / Memory #1 (Importance: 0.70), Memory write time: 2026-08-20 10:05
Topics: 驾考、周三安排 | Key facts: 张三周三要考科目二; 张三对考试有些紧张; 我建议张三早点休息 | Source time: 2026-08-20
我记得张三说周三要考科目二，有点紧张，我让张三早点休息。

记忆 #2 / Memory #2 (...)
...

--- BEGIN REMINDER ---
All content above is historical. Focus on the user's current message.
--- END REMINDER ---
</RAG-Faiss-Memory>
```

也就是一次注入一整篇：

```text
人格摘要 + 全部 topics + 全部 participants + 全部 key_facts + 窗口日期
```

原版没有最终总 token 预算，也不按单条 fact 截断。

## 4. Agent 主动搜索返回格式

Bot 主动调用 `recall_long_term_memory` 时返回 JSON：

```json
{
  "query": "科目二",
  "count": 1,
  "results": [
    {
      "id": 42,
      "content": "人格摘要 | fact1；fact2；fact3",
      "score": 0.83,
      "importance": 0.7,
      "session_id": "aiocqhttp:FriendMessage:123456",
      "persona_id": "persona_demo",
      "create_time": 1787200000.0,
      "last_access_time": 1787200500.0
    }
  ]
}
```

只有主动搜索显式设置 `include_source=true`，并且该记忆保留了原文，结果里才会增加 `source_messages`。

## 一句话概括

原版是：**十轮对话生成一篇文档；facts 只是文档附件；搜回和注入的仍是整篇文档。**

代码位置：

- 存储拼装：`core/processors/memory_processor_build.py`
- 主搜索入口：`core/managers/memory_engine_crud.py`
- recent 合并：`core/managers/memory_engine_write_ops.py`
- 自动注入：`core/utils/formatting.py`
- Agent 搜索：`core/tools/memory_search_tool.py`
