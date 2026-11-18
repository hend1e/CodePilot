# backend/agents/testgen/nodes.py
# 单测生成子图节点（对应课件第 6 章「三轨并行 + Human-in-the-Loop」的代码题那一轨）：
#   generate（LLM 生成）→ sandbox（Docker 断网执行）→ 失败回灌（≤2 轮）→ gate（interrupt 人工确认）

import asyncio
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import interrupt

from backend.agents.review.diff import parse_diff
from backend.agents.review.persist import mark_status, save_test_run
from backend.agents.testgen.prompts import TESTGEN_SYSTEM, build_testgen_prompt
from backend.agents.testgen.state import TEST_PATH, TestGenState
from backend.config import get_settings
from backend.core.llm_factory import get_llm
from backend.core.logger import get_logger
from backend.core.sandbox import SandboxRunner

logger = get_logger(__name__)
settings = get_settings()


def route_after_generate(state: TestGenState) -> str:
    return "sandbox" if state.get("test_patch") else "publish"


def route_after_sandbox(state: TestGenState) -> str:
    report = state.get("sandbox_report") or {}
    rounds = state.get("testgen_rounds", 0)
    if report.get("status") == "failed" and rounds < 2:
        return "testgen"          # 失败回灌：最多再生成 2 轮
    if report.get("status") == "passed":
        return "human_gate"       # 自动生成的测试必须人工确认
    return "publish"              # 环境失败 → 降级为「建议人工补测」


# ── 单测生成 ──────────────────────────────────────────────────
async def generate(state: TestGenState) -> dict:
    bundle = _bundle(state)
    symbols = bundle.added_python_symbols()
    if not symbols:
        return {"test_patch": None, "steps": state.get("steps", 0) + 1}

    rounds = state.get("testgen_rounds", 0)
    llm = get_llm("testgen", temperature=0.2)
    if llm is None:
        return {"test_patch": None, "steps": state.get("steps", 0) + 1}

    feedback = ""
    report = state.get("sandbox_report") or {}
    if rounds and report.get("status") == "failed":
        feedback = ("\n\n# 上一轮测试执行失败，请修正：\n"
                    f"{(report.get('stdout_tail') or '')[-1500:]}")

    try:
        async with asyncio.timeout(120):
            resp = await llm.ainvoke([
                SystemMessage(content=TESTGEN_SYSTEM),
                HumanMessage(content=build_testgen_prompt(symbols, bundle.text(40_000)) + feedback),
            ])
        code = _strip_fence(resp.content)
        if "def test_" not in code:
            raise ValueError("生成结果中没有测试函数")
    except Exception as e:      # noqa: BLE001
        logger.warning("testgen.degraded", error=str(e))
        return {"test_patch": None, "steps": state.get("steps", 0) + 1,
                "open_questions": state.get("open_questions", []) + ["单测生成失败，建议人工补测"]}

    return {
        "test_patch": code,
        "test_targets": [s["symbol"] for s in symbols][:10],
        "testgen_rounds": rounds + 1,
        "steps": state.get("steps", 0) + 1,
    }


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:python)?\s*\n(.*)\n```$", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


# ── 沙箱执行 ──────────────────────────────────────────────────
async def sandbox(state: TestGenState) -> dict:
    patch = state.get("test_patch") or ""
    repo_path = state.get("repo", {}).get("local_path")
    runner = SandboxRunner()
    result = await runner.run(repo_path, {TEST_PATH: patch})
    report = {**result.as_dict(), "image": settings.sandbox_image}
    await save_test_run(state["task_id"], patch, report, state.get("test_targets", []))
    logger.info("testgen.sandbox", task_id=state["task_id"], status=report["status"],
                passed=report["passed"], failed=report["failed"])
    return {
        "sandbox_report": report,
        "degraded": state.get("degraded", False) or report["status"] in ("error", "skipped"),
        "steps": state.get("steps", 0) + 1,
    }


# ── Human-in-the-Loop：interrupt + resume ──────────────────────
async def gate(state: TestGenState) -> dict:
    report = state.get("sandbox_report") or {}
    payload = {
        "type": "test_approval",
        "task_id": state["task_id"],
        "question": "自动生成的单测已在沙箱通过，是否合入？",
        "test_patch": (state.get("test_patch") or "")[:20_000],
        "sandbox": {k: report.get(k) for k in ("status", "passed", "failed", "coverage")},
        "targets": state.get("test_targets", []),
    }
    decision = interrupt(payload)         # 图在此挂起，等待 /resume
    await mark_status(state["task_id"], "running")
    return {"hitl": decision, "steps": state.get("steps", 0) + 1}


