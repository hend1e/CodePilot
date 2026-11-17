# backend/agents/orchestrator/graph.py
# 主图：统一入口 → 路由 → 四个子图之一 → 汇总。
# 子图通过普通节点函数调用（各自 compile 一次），主图只负责「选哪条链路」。
# 这样主图永远很短，新场景只需加一个 branch + 一条条件边。

import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agents.defect.graph import build_defect_graph
from backend.agents.orchestrator.router import classify
from backend.agents.rag.graph import ask as rag_ask
from backend.agents.review.state import summarize
from backend.agents.testgen.graph import build_testgen_graph
from backend.core.logger import get_logger
from backend.services.review_service import run_review_task

logger = get_logger(__name__)

INTENTS = ["review", "qa", "test", "defect"]


class OrchestratorState(TypedDict, total=False):
    message: str
    tenant_id: str
    task_id: str
    repo_id: str | None
    repo: dict
    pr: dict
    diff_text: str
    stack_trace: str
    intent: str
    route_reason: str
    result: dict
    events: Annotated[list[dict], operator.add]
    error: str | None


async def route(state: OrchestratorState) -> dict:
    decision = await classify(state["message"])
    return {
        "intent": decision.intent,
        "route_reason": decision.reason,
        "events": [{"stage": "route", **decision.as_dict()}],
    }


def branch_review(state: OrchestratorState) -> str:
    return "review"


async def review_branch(state: OrchestratorState) -> dict:
    """PR 审查：真正执行四维 fan-out 子图（含单测生成与 HitL）。"""
    final = await run_review_task(state["task_id"])
    summary = final.get("summary") or {}
    return {"result": {"kind": "review", "task_id": state["task_id"], "summary": summary,
                       "final": final},
            "events": [{"stage": "review_done", "mergeable": summary.get("mergeable", True)}]}


async def rag_branch(state: OrchestratorState) -> dict:
    out = await rag_ask(state["message"], state.get("repo_id"),
                        state.get("tenant_id", "tenant_default"))
    return {"result": {"kind": "qa", "answer": out.get("answer", ""),
                       "citations": out.get("citations", []),
                       "degraded": out.get("degraded", False)},
            "events": [{"stage": "rag_done", "citations": len(out.get("citations", []))}]}


async def test_branch(state: OrchestratorState) -> dict:
    graph = build_testgen_graph()
    out = await graph.ainvoke({
        "task_id": state["task_id"], "tenant_id": state.get("tenant_id"),
        "repo": state.get("repo", {}), "diff_text": state.get("diff_text", ""),
        "testgen_rounds": 0,
    }, config={"configurable": {"thread_id": f"{state['task_id']}:testgen"}})
    return {"result": {"kind": "test", "test_patch": out.get("test_patch"),
                       "sandbox": out.get("sandbox_report"), "hitl": out.get("hitl")},
            "events": [{"stage": "test_done",
                        "sandbox": (out.get("sandbox_report") or {}).get("status")}]}


async def defect_branch(state: OrchestratorState) -> dict:
    graph = build_defect_graph()
    out = await graph.ainvoke({
        "task_id": state.get("task_id"), "tenant_id": state.get("tenant_id"),
        "repo": state.get("repo", {}), "stack_trace": state.get("stack_trace")
        or state["message"], "steps": 0,
    })
    return {"result": {"kind": "defect", **(out.get("final") or {})},
            "events": [{"stage": "defect_done"}]}


def build_orchestrator_graph():
    builder = StateGraph(OrchestratorState)
    builder.add_node("route", route)
    builder.add_node("review", review_branch)
    builder.add_node("qa", rag_branch)
    builder.add_node("test", test_branch)
    builder.add_node("defect", defect_branch)

    builder.add_edge(START, "route")
    builder.add_conditional_edges("route", lambda s: s.get("intent", "qa"), INTENTS)
    for node in INTENTS:
        builder.add_edge(node, END)
    return builder.compile()


def quick_summary(findings: list) -> dict:
    return summarize(findings)
