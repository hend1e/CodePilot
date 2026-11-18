# backend/agents/testgen/graph.py
# 单测生成子图：generate → sandbox →（失败回灌 ≤2 轮）→ gate（interrupt 人工确认）→ done。
# 节点实现在 nodes.py，State 在 state.py —— 与课件「每个 Agent 一个文件夹」的结构一致。

from langgraph.graph import END, START, StateGraph

from backend.agents.review.persist import mark_status
from backend.agents.testgen.nodes import (
    gate,
    generate,
    route_after_generate,
    route_after_sandbox,
    sandbox,
)
from backend.agents.testgen.state import TestGenState
from backend.db.session import get_checkpointer


async def done(state: TestGenState) -> dict:
    await mark_status(state["task_id"], "done")
    return {}


def build_testgen_graph(checkpointer=None):
    builder = StateGraph(TestGenState)
    builder.add_node("generate", generate)
    builder.add_node("sandbox", sandbox)
    builder.add_node("gate", gate)
    builder.add_node("done", done)

    builder.add_edge(START, "generate")
    builder.add_conditional_edges("generate", route_after_generate, ["sandbox", "done"])
    builder.add_conditional_edges("sandbox", route_after_sandbox, ["generate", "gate", "done"])
    builder.add_edge("gate", "done")
    builder.add_edge("done", END)

    return builder.compile(checkpointer=checkpointer or get_checkpointer())
