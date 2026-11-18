# backend/agents/qa/graph.py
# 代码库 RAG 子图：rewrite → retrieve → answer → persist（线性，无分支）。

from langgraph.graph import END, START, StateGraph

from backend.agents.qa.nodes import answer, persist, retrieve, rewrite_query
from backend.agents.qa.state import RAGState


def build_rag_graph(checkpointer=None):
    builder = StateGraph(RAGState)
    builder.add_node("rewrite", rewrite_query)
    builder.add_node("retrieve", retrieve)
    builder.add_node("answer", answer)
    builder.add_node("persist", persist)

    builder.add_edge(START, "rewrite")
    builder.add_edge("rewrite", "retrieve")
    builder.add_edge("retrieve", "answer")
    builder.add_edge("answer", "persist")
    builder.add_edge("persist", END)

    return builder.compile(checkpointer=checkpointer)


async def ask(question: str, repo_id: str | None = None,
              tenant_id: str = "tenant_default", task_id: str | None = None,
              thread_id: str | None = None, memory: str = "") -> dict:
    """便捷入口：不落 checkpoint（一次性问答）。

    memory：历史摘要（课件 5.9），由调用方从 core.memory 取出后注入。
    """
    graph = build_rag_graph()
    return await graph.ainvoke({
        "question": question, "repo_id": repo_id, "tenant_id": tenant_id, "task_id": task_id,
        "thread_id": thread_id, "memory": memory,
    })
