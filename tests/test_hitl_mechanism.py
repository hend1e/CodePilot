"""验证 LangGraph interrupt / resume 机制（不依赖 DB / LLM）。

human_gate 用的就是这套机制：interrupt() 挂起 → checkpointer 保存 → Command(resume=...) 继续。
"""

from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class S(TypedDict, total=False):
    patch: str
    decision: dict
    done: bool


async def gate(state: S) -> dict:
    decision = interrupt({"question": "merge?", "patch": state.get("patch")})
    return {"decision": decision}


async def finish(state: S) -> dict:
    return {"done": True}


def build():
    b = StateGraph(S)
    b.add_node("gate", gate)
    b.add_node("finish", finish)
    b.add_edge(START, "gate")
    b.add_edge("gate", "finish")
    b.add_edge("finish", END)
    return b.compile(checkpointer=InMemorySaver())


@pytest.mark.asyncio
async def test_interrupt_then_resume():
    graph = build()
    cfg = {"configurable": {"thread_id": "t-1"}}

    out = await graph.ainvoke({"patch": "def test_x(): ..."}, config=cfg)
    assert out.get("__interrupt__"), "应在此挂起"
    payload = out["__interrupt__"][0].value
    assert payload["question"] == "merge?"

    resumed = await graph.ainvoke(Command(resume={"action": "approve"}), config=cfg)
    assert resumed["decision"] == {"action": "approve"}
    assert resumed["done"] is True


@pytest.mark.asyncio
async def test_resume_payload_isolated_per_thread():
    graph = build()
    await graph.ainvoke({"patch": "a"}, config={"configurable": {"thread_id": "t-a"}})
    out_b = await graph.ainvoke({"patch": "b"}, config={"configurable": {"thread_id": "t-b"}})
    assert out_b["__interrupt__"][0].value["patch"] == "b"
