# backend/agents/qa/state.py
# 代码库问答子图的 State（对应课件 5.11 State 与 Prompts）。

from typing import TypedDict


class RAGState(TypedDict, total=False):
    question: str
    repo_id: str | None
    tenant_id: str
    task_id: str | None
    thread_id: str | None        # 多轮记忆（课件 5.9）
    memory: str                  # 历史摘要，注入 prompt
    question_type: dict          # 意图分类结果（课件 5.8）
    queries: list[str]
    chunks: list[dict]
    answer: str
    citations: list[dict]
    degraded: bool
    invalid_citations: int
