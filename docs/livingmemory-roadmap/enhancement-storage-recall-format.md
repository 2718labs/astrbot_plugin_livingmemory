---
title: Enhancement fork 的存储与召回格式
status: Working Tree Reference
updated: 2026-08-22
baseline_commit: 3b5f2b3
---

# Enhancement fork 的存储与召回格式

下面只列实际落库、搜索结果和最终注入。值是示例，字段和拼装方式来自当前工作树。本例中的张三是虚构用户，`persona_demo` 是虚构人格 ID。

## 1. 实际落库

原来的 `documents` 表仍保留一个 parent 外壳，但生产搜索改用独立的 canonical fact。

```json
{
  "documents": {
    "id": 42,
    "text": "张三将在2026年8月26日参加科目二考试",
    "metadata": {
      "memory_schema_version": "v3",
      "parent_id": "memory_<stable hash>",
      "idempotency_key": "idem_<stable hash>",
      "canonical_summary": "张三将在2026年8月26日参加科目二考试",
      "source_window": {
        "fingerprint": "source_<stable hash>",
        "message_ids": [12345],
        "message_count": 1
      },
      "key_facts": [
        {
          "fact_id": "fact_<stable hash>",
          "parent_id": "memory_<stable hash>",
          "fact": "张三将在2026年8月26日参加科目二考试",
          "topics": ["驾考"],
          "topic_refs": [
            {
              "topic_id": "topic_<stable hash>",
              "raw_name": "驾考",
              "name": "驾考",
              "decision": "created"
            }
          ],
          "participants": ["张三"],
          "participant_refs": [
            {
              "participant_id": "<stable identity>",
              "name": "张三",
              "identity_key": "<stable identity>",
              "source": "message_sender"
            }
          ],
          "importance": 0.8,
          "persona_reaction": {
            "emotion": "有些担心",
            "thought": "希望张三顺利通过"
          }
        }
      ]
    }
  },
  "memory_parents": {
    "parent_id": "memory_<stable hash>",
    "document_id": 42,
    "overview": "张三将在2026年8月26日参加科目二考试",
    "status": "active"
  },
  "memory_facts": {
    "fact_id": "fact_<stable hash>",
    "parent_id": "memory_<stable hash>",
    "fact_json": "<上面的完整 fact 对象>",
    "search_text": "张三将在2026年8月26日参加科目二考试 张三 驾考",
    "importance": 0.8,
    "status": "active",
    "retrieval_count": 0,
    "injection_count": 0
  }
}
```

FTS 和向量库只索引 `search_text`。`persona_reaction`、parent summary 和同组其他 facts 不进入事实搜索文本。

## 2. 搜索结果

`MemoryEngine.search_memories()` 返回命中的单条 fact：

```python
HybridResult(
    doc_id=42,
    final_score=0.86,
    rrf_score=0.0,
    bm25_score=-2.1,
    vector_score=0.91,
    content="张三将在2026年8月26日参加科目二考试",
    metadata={
        "memory_schema_version": "v3",
        "fact_id": "fact_<stable hash>",
        "parent_id": "memory_<stable hash>",
        "session_id": "aiocqhttp:FriendMessage:123456",
        "persona_id": "persona_demo",
        "importance": 0.8,
        "status": "active",
        "persona_reaction": {
            "emotion": "有些担心",
            "thought": "希望张三顺利通过"
        },
        "has_source": True,
        "retrieval_route": "fact",
        "selection_reason": "relevance_threshold_passed"
    },
    score_breakdown={...}
)
```

`doc_id` 只用于追溯 parent；真正被召回的是 `content` 里的单条事实。结果可以为空，不再用 recent 记忆凑数。

## 3. 最终注入

候选经过相关性和预算检查后，写给模型的是：

```text
<RAG-Faiss-Memory>
--- BEGIN HISTORICAL MEMORY REFERENCE ---
以下是与当前问题直接相关的历史事实，仅作背景；若与用户当前说法冲突，以当前消息为准。
--- END HISTORICAL MEMORY REFERENCE ---

- 张三将在2026年8月26日参加科目二考试
  当时反应：有些担心；希望张三顺利通过

--- BEGIN REMINDER ---
自然使用确有帮助的事实，不要主动宣布、逐条复述或炫耀记忆。
--- END REMINDER ---
</RAG-Faiss-Memory>
```

最终注入不包含 parent summary、topics、participants、fact ID、分数或同组其他 facts。默认总预算 1600 token、单条 fact 预算 150 token；放不下就整条不注入。

相对时间在总结阶段直接改写进 fact 正文；消息发送时间保留在来源记录中。当前没有独立事件时间字段，也不按时间过滤或排序记忆。

一句话：**原版搜回整篇十轮总结；当前 fork 搜回并注入单条 fact。**
