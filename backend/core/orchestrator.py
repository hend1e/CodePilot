# backend/core/orchestrator.py
# 统一入口编排（对应课件第 8 章）：
#   规则前置拦截（零 token）→ LLM 意图分类 → 分发到 review / qa / test 子图
# 主图只负责「选哪条链路」，业务都在子图里；新增场景 = 加一个 branch + 一条条件边。

import asyncio
import operator
import re
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from backend.agents.qa.prompts import INTENT_SYSTEM
from backend.agents.review.state import summarize
from backend.agents.testgen.graph import build_testgen_graph
from backend.config import get_settings
from backend.core.llm_factory import get_structured_llm
from backend.core.logger import get_logger
from backend.core.review_service import run_review_task

logger = get_logger(__name__)
settings = get_settings()

INTENTS = ["review", "qa", "test"]

PR_URL_RE = re.compile(r"https?://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+)")
REVIEW_WORDS = ("审查", "评审", "review", "看看这个 pr", "看下这个 pr", "diff", "改动")
TEST_WORDS = ("生成单测", "写测试", "补测试", "test case", "单元测试")


PR_URL_RE = re.compile(r"https?://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+)")
REVIEW_WORDS = ("审查", "评审", "review", "看看这个 pr", "看下这个 pr", "diff", "改动")
TEST_WORDS = ("生成单测", "写测试", "补测试", "test case", "单元测试")


class IntentResult:
    def __init__(self, intent: str, repo: str | None, reason: str, confidence: float,
                 source: str, pr_number: int | None = None):
        self.intent, self.repo, self.reason = intent, repo, reason
        self.confidence, self.source, self.pr_number = confidence, source, pr_number

    def as_dict(self) -> dict:
        return {"intent": self.intent, "repo": self.repo, "reason": self.reason,
                "confidence": self.confidence, "source": self.source,
                "pr_number": self.pr_number}


def rule_route(message: str) -> IntentResult | None:
    text = message.strip()
    m = PR_URL_RE.search(text)
    if m:
        return IntentResult("review", m.group("repo"), "PR 链接", 0.99, "rule", int(m.group("num")))
    if any(w in text.lower() for w in TEST_WORDS):
        return IntentResult("test", None, "命中单测生成关键词", 0.8, "rule")
    if any(w in text.lower() for w in REVIEW_WORDS) and "```" in text:
        return IntentResult("review", None, "包含 diff 代码块", 0.75, "rule")
    return None


class _IntentOut(BaseModel):
    intent: str = "qa"
    repo: str | None = None
    reason: str = ""


async def classify(message: str) -> IntentResult:
    hit = rule_route(message)
    if hit:
        logger.info("router.rule_hit", intent=hit.intent, reason=hit.reason)
        return hit

    llm = get_structured_llm("router", _IntentOut)
    if llm is None:
        return IntentResult("qa", None, "无 LLM，默认问答", 0.4, "default")

    try:
        async with asyncio.timeout(20):
            out = await llm.ainvoke([
                SystemMessage(content=INTENT_SYSTEM),
                HumanMessage(content=message[:4000]),
            ])
        intent = out.intent if out.intent in ("review", "qa", "test") else "qa"
        return IntentResult(intent, out.repo or None, out.reason or "LLM 分类", 0.8, "llm")
    except Exception as e:      # noqa: BLE001
        logger.warning("router.degraded", error=str(e)[:200])
        return IntentResult("qa", None, "分类失败，兜底问答", 0.3, "default")


# ──────────────────────────────────────────────────────────────
# 主图：route → 三个子图之一
# ──────────────────────────────────────────────────────────────

class OrchestratorState(TypedDict, total=False):
    message: str
    tenant_id: str
    task_id: str
    repo_id: str | None
    repo: dict
    pr: dict
    diff_text: str
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


async def review_branch(state: OrchestratorState) -> dict:
    """PR 审查：执行四维 fan-out 子图（含单测生成与 HitL）。"""
    final = await run_review_task(state["task_id"])
    summary = final.get("summary") or {}
    return {"result": {"kind": "review", "task_id": state["task_id"], "summary": summary,
                       "final": final},
            "events": [{"stage": "review_done", "mergeable": summary.get("mergeable", True)}]}


async def qa_branch(state: OrchestratorState) -> dict:
    from backend.agents.qa.graph import ask as qa_ask

    out = await qa_ask(state["message"], state.get("repo_id"),
                       state.get("tenant_id", settings.default_tenant_id))
    return {"result": {"kind": "qa", "answer": out.get("answer", ""),
                       "citations": out.get("citations", []),
                       "degraded": out.get("degraded", False)},
            "events": [{"stage": "qa_done", "citations": len(out.get("citations", []))}]}


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


def build_orchestrator_graph():
    builder = StateGraph(OrchestratorState)
    builder.add_node("route", route)
    builder.add_node("review", review_branch)
    builder.add_node("qa", qa_branch)
    builder.add_node("test", test_branch)

    builder.add_edge(START, "route")
    builder.add_conditional_edges("route", lambda s: s.get("intent", "qa"), INTENTS)
    for node in INTENTS:
        builder.add_edge(node, END)
    return builder.compile()
