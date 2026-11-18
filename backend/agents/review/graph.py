# backend/agents/review/graph.py
# PR 审查子图：确定性主干 + 单点 fan-out（Send）+ 条件边 + HitL 挂起点。
# 复杂逻辑都在节点里，图本身要一眼能看懂 —— 这是「确定性编排 + 有限自主」的落地。

from langgraph.graph import END, START, StateGraph

from backend.agents.review.nodes import (
    aggregate,
    fanout_dimensions,
    human_gate,
    ingest,
    publish,
    retrieve_ctx,
    review_one,
    route_after_aggregate,
    route_after_sandbox,
    route_after_testgen,
    sandbox_node,
    testgen,
    triage,
)
from backend.agents.review.state import ReviewState
from backend.db.session import get_checkpointer


def build_review_graph(checkpointer=None, *, with_hitl: bool = True):
    """编译审查图。with_hitl=False 用于不需要人工确认的批量预审场景。"""
    builder = StateGraph(ReviewState)

    builder.add_node("ingest", ingest)
    builder.add_node("triage", triage)
    builder.add_node("retrieve_ctx", retrieve_ctx)
    builder.add_node("review_one", review_one)
    builder.add_node("aggregate", aggregate)
    builder.add_node("testgen", testgen)
    builder.add_node("sandbox", sandbox_node)
    builder.add_node("publish", publish)
    if with_hitl:
        builder.add_node("human_gate", human_gate)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "triage")
    builder.add_conditional_edges("triage", lambda s: "retrieve_ctx",
                                  ["retrieve_ctx", "publish"])
    builder.add_conditional_edges("retrieve_ctx", fanout_dimensions, ["review_one"])
    builder.add_edge("review_one", "aggregate")
    builder.add_conditional_edges("aggregate", route_after_aggregate,
                                  ["testgen", "publish"])
    builder.add_conditional_edges("testgen", route_after_testgen, ["sandbox", "publish"])
    targets = ["human_gate", "publish"] if with_hitl else ["publish", "testgen"]
    builder.add_conditional_edges("sandbox", route_after_sandbox, targets)
    if with_hitl:
        builder.add_edge("human_gate", "publish")
    builder.add_edge("publish", END)

    return builder.compile(checkpointer=checkpointer or get_checkpointer())
