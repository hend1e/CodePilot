# backend/agents/defect/graph.py
# 缺陷定位子图：栈帧解析 → 检索 → 排序 → 建议（低置信时 abstain）。

import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from backend.agents.review.nodes import defect_locate
from backend.core.logger import get_logger
from backend.db.session import AsyncSessionLocal

logger = get_logger(__name__)


class DefectState(TypedDict, total=False):
    task_id: str
    tenant_id: str
    repo: dict
    stack_trace: str
    final: dict | None
    steps: int


async def persist_defect(state: DefectState) -> dict:
    defect = (state.get("final") or {}).get("defect") or {}
    candidates = defect.get("candidates") or []
    confidence = max((c.get("confidence", 0) for c in candidates), default=0.0)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO defect_reports (task_id, stack_trace, candidates, confidence, suggestion)
                    VALUES (:task_id, :trace, CAST(:cand AS JSONB), :conf, :sug)
                """),
                {"task_id": state.get("task_id"), "trace": state.get("stack_trace", "")[:20_000],
                 "cand": json.dumps(candidates, ensure_ascii=False), "conf": confidence,
                 "sug": defect.get("fix_suggestion", "")},
            )
            await session.commit()
    except Exception as e:      # noqa: BLE001
        logger.warning("defect.persist_failed", error=str(e)[:200])
    return {}


def build_defect_graph(checkpointer=None):
    builder = StateGraph(DefectState)
    builder.add_node("locate", defect_locate)
    builder.add_node("persist", persist_defect)
    builder.add_edge(START, "locate")
    builder.add_edge("locate", "persist")
    builder.add_edge("persist", END)
    return builder.compile(checkpointer=checkpointer)
