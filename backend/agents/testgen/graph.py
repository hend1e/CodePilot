# backend/agents/testgen/graph.py
# 单测生成子图：生成 → 沙箱执行 → 失败回灌（≤2 轮）→ 人工确认（interrupt）。
# 复用审查子图里的 testgen / sandbox 节点实现，保证两条链路行为一致。

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend.agents.review.nodes import sandbox_node, testgen
from backend.agents.review.persist import mark_status
from backend.config import get_settings
from backend.core.logger import get_logger
from backend.db.session import get_checkpointer

logger = get_logger(__name__)
settings = get_settings()


class TestGenState(TypedDict, total=False):
    """复用 review 节点所需字段（字段名与 ReviewState 保持一致）。"""
    task_id: str
    tenant_id: str
    repo: dict
    diff_text: str
    test_patch: str | None
    test_targets: list[str]
    testgen_rounds: int
    sandbox_report: dict | None
    hitl: dict | None
    steps: int
    degraded: bool
    open_questions: list[str]


def route_after_sandbox(state: dict) -> str:
    report = state.get("sandbox_report") or {}
    if report.get("status") == "failed" and state.get("testgen_rounds", 0) < 2:
        return "generate"
    if report.get("status") == "passed":
        return "gate"
    return "done"


async def gate(state: dict) -> dict:
    decision = interrupt({
        "type": "test_approval",
        "task_id": state.get("task_id"),
        "question": "单测已在沙箱通过，是否合入？",
        "test_patch": (state.get("test_patch") or "")[:20_000],
        "sandbox": {k: (state.get("sandbox_report") or {}).get(k)
                    for k in ("status", "passed", "failed", "coverage")},
    })
    return {"hitl": decision}


async def done(state: dict) -> dict:
    await mark_status(state["task_id"], "done")
    return {}


def build_testgen_graph(checkpointer=None):
    builder = StateGraph(TestGenState)
    builder.add_node("generate", testgen)
    builder.add_node("sandbox", sandbox_node)
    builder.add_node("gate", gate)
    builder.add_node("done", done)

    builder.add_edge(START, "generate")
    builder.add_conditional_edges("generate", lambda s: "sandbox" if s.get("test_patch") else "done",
                                  ["sandbox", "done"])
    builder.add_conditional_edges("sandbox", route_after_sandbox, ["generate", "gate", "done"])
    builder.add_edge("gate", "done")
    builder.add_edge("done", END)

    return builder.compile(checkpointer=checkpointer or get_checkpointer())
