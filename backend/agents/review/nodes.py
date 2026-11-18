# backend/agents/review/nodes.py
# PR 审查子图的节点实现。设计原则：
#   ① 主干确定（ingest → triage → retrieve → fan-out → aggregate），只有决策点用条件边；
#   ② 任何节点失败都转成结构化状态（dim_status / degraded / open_questions），不裸抛异常；
#   ③ 所有 LLM 结论必须过证据校验，聚合阶段再做跨维度仲裁与去重。

import asyncio
import json
import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel
from langgraph.types import interrupt

from backend.agents.rag.retrieval import retrieve_context
from backend.agents.review.diff import DiffBundle, is_test_path, parse_diff
from backend.agents.review.persist import mark_status, save_final, save_findings, save_test_run
from backend.agents.review.prompts import (
    TESTGEN_SYSTEM, TRIAGE_SYSTEM, build_testgen_prompt,
)
from backend.agents.review.reviewers import build_reviewer
from backend.agents.review.rules import validate_evidence
from backend.agents.review.state import Finding, ReviewState, summarize
from backend.config import get_settings
from backend.core.exceptions import InvalidInput
from backend.core.llm_factory import estimate_cost_usd, get_llm, get_structured_llm
from backend.core.logger import get_logger
from backend.core.retry import estimate_tokens
from backend.infra import github as gh
from backend.infra.sandbox import SandboxRunner

logger = get_logger(__name__)
settings = get_settings()

TEST_PATH = "tests/test_generated.py"


def _bundle(state: ReviewState) -> DiffBundle:
    text = state.get("diff_text") or ""
    if not text and state.get("diff"):
        return parse_diff(state["diff"].get("raw", ""))
    return parse_diff(text)


# ── ingest：确定性节点（不做任何 LLM 调用）──────────────────────
async def ingest(state: ReviewState) -> dict:
    text = state.get("diff_text") or ""
    if not text and state.get("pr", {}).get("number") and state.get("repo", {}).get("provider") == "github":
        try:
            text = await gh.fetch_pr_diff(state["repo"]["full_name"], state["pr"]["number"])
        except Exception as e:      # noqa: BLE001
            raise InvalidInput(f"拉取 PR diff 失败: {e}") from e

    bundle = parse_diff(text)
    if not bundle:
        raise InvalidInput("diff 为空或无法解析")
    if bundle.added_lines > 20_000:
        raise InvalidInput(f"diff 过大（{bundle.added_lines} 行新增），请拆分 PR")

    await mark_status(state["task_id"], "running")
    logger.info("review.ingested", task_id=state["task_id"], files=len(bundle.files),
                added=bundle.added_lines)
    return {
        "diff_text": text,
        "diff": {"files": [f.path for f in bundle.files], "added_lines": bundle.added_lines,
                 "removed_lines": bundle.removed_lines},
        "budget": {
            "max_tokens": settings.review_max_tokens,
            "max_cost_usd": settings.review_max_cost_usd,
            "max_steps": settings.review_max_steps,
            "tokens_used": 0,
            "cost_usd": 0.0,
        },
        "steps": 1,
        "no_progress": 0,
        "findings": [],
        "dim_status": {},
        "usage": [],
        "testgen_rounds": 0,
        "degraded": not settings.llm_enabled,
    }


# ── triage：规则为主，LLM 为辅（失败不影响主流程）──────────────
_DOC_EXT = (".md", ".rst", ".txt")


async def triage(state: ReviewState) -> dict:
    bundle = _bundle(state)
    paths = bundle.paths
    is_docs = all(p.lower().endswith(_DOC_EXT) for p in paths) if paths else False
    is_test_only = all(is_test_path(p) for p in paths) if paths else False

    pr_type = "docs" if is_docs else ("test" if is_test_only else "feature")
    dimensions = ["security", "performance", "style", "test"]
    needs_ctx = any(
        k in p.lower() for p in paths
        for k in ("auth", "login", "permission", "db", "model", "service", "api", "core")
    )
    confidence = 0.6

    if settings.llm_enabled and not is_docs:
        llm = get_structured_llm("triage", _TriageOut)
        if llm is not None:
            try:
                async with asyncio.timeout(30):
                    out = await llm.ainvoke([
                        SystemMessage(content=TRIAGE_SYSTEM),
                        HumanMessage(content=json.dumps({
                            "files": paths[:60],
                            "added_lines": bundle.added_lines,
                            "removed_lines": bundle.removed_lines,
                        }, ensure_ascii=False)),
                    ])
                pr_type = out.pr_type
                dimensions = [d for d in out.chain if d in dimensions] or dimensions
                needs_ctx = bool(out.needs_ctx)
                confidence = 0.85
            except Exception as e:      # noqa: BLE001 —— 分诊失败降级到默认策略
                logger.warning("triage.degraded", error=str(e))

    if is_docs:
        dimensions = ["style"]

    return {
        "triage": {"pr_type": pr_type, "chain": dimensions, "needs_ctx": needs_ctx,
                   "confidence": confidence,
                   "size": {"files": len(paths), "added": bundle.added_lines}},
        "steps": state.get("steps", 0) + 1,
    }


class _TriageOut(BaseModel):
    pr_type: str = "feature"
    chain: list[str] = ["security", "performance", "style", "test"]
    needs_ctx: bool = False
    reason: str = ""


# ── retrieve_ctx：RAG 检索内部规范/相似代码；失败可降级 ──────────
async def retrieve_ctx(state: ReviewState) -> dict:
    triage_info = state.get("triage", {})
    if not triage_info.get("needs_ctx"):
        return {"repo_ctx": {"chunks": [], "degraded": False},
                "steps": state.get("steps", 0) + 1}

    bundle = _bundle(state)
    query = " ".join(bundle.paths[:8]) + " " + bundle.added_python_symbols().__str__()[:200]
    try:
        ctx = await retrieve_context(
            query=query,
            repo_id=state.get("repo", {}).get("id"),
            tenant_id=state.get("tenant_id", settings.default_tenant_id),
            top_k=min(6, settings.rerank_top_k),
        )
        return {"repo_ctx": ctx, "steps": state.get("steps", 0) + 1}
    except Exception as e:      # noqa: BLE001
        logger.warning("retrieve.degraded", task_id=state.get("task_id"), error=str(e))
        return {"repo_ctx": {"chunks": [], "degraded": True, "error": str(e)[:200]},
                "degraded": True, "steps": state.get("steps", 0) + 1}


def _ctx_text(state: ReviewState) -> str:
    chunks = state.get("repo_ctx", {}).get("chunks", [])
    return "\n\n".join(
        f"[{c['path']}:{c['start_line']}-{c['end_line']}] {c.get('symbol', '')}\n{c['content'][:1200]}"
        for c in chunks
    )


# ── fan-out：Send 并行扇出四个维度 ────────────────────────────
def fanout_dimensions(state: ReviewState) -> list:
    from langgraph.types import Send

    dims = state.get("triage", {}).get("chain") or list(settings.dim_timeouts)
    return [
        Send("review_one", {
            "dimension": d,
            "diff_text": state.get("diff_text", ""),
            "repo_ctx_text": _ctx_text(state),
            "pr_meta": {"repo": state.get("repo", {}).get("full_name", ""),
                        **state.get("pr", {})},
            "budget": state.get("budget", {}),
        })
        for d in dims
    ]


async def review_one(payload: dict) -> dict:
    """单个维度的审查。payload 来自 Send；返回值合并回父状态。"""
    dim = payload["dimension"]
    bundle = parse_diff(payload.get("diff_text", ""))
    reviewer = build_reviewer(dim)

    findings, status, usage = await reviewer.run(
        bundle, payload.get("repo_ctx_text", ""), payload.get("pr_meta", {}),
        payload.get("budget", {}),
    )
    return {
        "findings": findings,
        "dim_status": {dim: status},
        "usage": [{"dimension": dim, **usage}],
        "degraded": status != "ok",
    }


# ── aggregate：去重 → 跨维仲裁 → 定级 ─────────────────────────
def _arbitrate(findings: list[Finding]) -> list[Finding]:
    """同一位置同一问题的跨维度重复：保留严重度最高的那条，合并维度信息。"""
    best: dict[tuple, Finding] = {}
    for f in findings:
        key = (f.file_path, f.line_start, re.sub(r"\W+", "", f.message)[:24])
        cur = best.get(key)
        if cur is None or (f.severity, f.confidence) > (cur.severity, cur.confidence):
            best[key] = f
    return sorted(best.values(), key=lambda x: (
        {"block": 0, "warn": 1, "info": 2}[x.severity], x.file_path, x.line_start))


async def aggregate(state: ReviewState) -> dict:
    findings = _arbitrate(state.get("findings", []))
    summary = summarize(findings)
    dim_status = state.get("dim_status", {})
    degraded = bool(state.get("degraded")) or any(v != "ok" for v in dim_status.values())

    # 预算：把并行节点的 usage 汇总（usage 用 operator.add 归并，是幂等的）
    usage = state.get("usage", [])
    prompt_tokens = sum(u.get("prompt_tokens", 0) for u in usage)
    completion = sum(u.get("completion_tokens", 0) for u in usage)
    cost = estimate_cost_usd(settings.llm_model_code, prompt_tokens, completion)
    budget = {**state.get("budget", {}), "tokens_used": prompt_tokens + completion,
              "cost_usd": cost}

    await save_findings(state["task_id"], findings)
    logger.info("review.aggregated", task_id=state["task_id"], findings=len(findings),
                blocks=summary["by_severity"]["block"], degraded=degraded,
                prompt_tokens=prompt_tokens, cost=cost)

    return {
        "findings": findings,
        "final": {
            "summary": summary,
            "dim_status": dim_status,
            "degraded": degraded,
            "pr_type": state.get("triage", {}).get("pr_type"),
            "thread_id": state.get("thread_id"),
        },
        "budget": budget,
        "degraded": degraded,
        "no_progress": state.get("no_progress", 0) + 1,
        "steps": state.get("steps", 0) + 1,
    }


# ── 路由函数 ──────────────────────────────────────────────────
def route_after_aggregate(state: ReviewState) -> str:
    symbols = _bundle(state).added_python_symbols()
    if symbols and settings.sandbox_mode != "off" and settings.llm_enabled:
        return "testgen"
    return "publish"


def route_after_testgen(state: ReviewState) -> str:
    return "sandbox" if state.get("test_patch") else "publish"


def route_after_sandbox(state: ReviewState) -> str:
    report = state.get("sandbox_report") or {}
    rounds = state.get("testgen_rounds", 0)
    if report.get("status") == "failed" and rounds < 2:
        return "testgen"          # 失败回灌：最多再生成 2 轮
    if report.get("status") == "passed":
        return "human_gate"       # 自动生成的测试必须人工确认
    return "publish"              # 环境失败 → 降级为「建议人工补测」


# ── 单测生成 ──────────────────────────────────────────────────
async def testgen(state: ReviewState) -> dict:
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
async def sandbox_node(state: ReviewState) -> dict:
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
async def human_gate(state: ReviewState) -> dict:
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


# ── publish：落库 + 回写评论 ───────────────────────────────────
async def publish(state: ReviewState) -> dict:
    findings = state.get("findings", [])
    summary = summarize(findings)
    report = state.get("sandbox_report")
    hitl = state.get("hitl")
    usage = state.get("usage", [])
    prompt_tokens = sum(u.get("prompt_tokens", 0) for u in usage)
    completion = sum(u.get("completion_tokens", 0) for u in usage)
    cost = estimate_cost_usd(settings.llm_model_code, prompt_tokens, completion)

    final = {
        **(state.get("final") or {}),
        "findings": [f.model_dump() for f in findings],
        "summary": summary,
        "dim_status": state.get("dim_status", {}),
        "test_report": report,
        "hitl": hitl,
        "degraded": bool(state.get("degraded")),
        "steps": state.get("steps", 0),
        "cost_usd": cost,
        "tokens": {"prompt": prompt_tokens, "completion": completion},
        "thread_id": state.get("thread_id"),
        "open_questions": state.get("open_questions", []),
    }

    await save_final(state["task_id"], final, degraded=final["degraded"],
                     steps=state.get("steps", 0), prompt_tokens=prompt_tokens,
                     completion_tokens=completion, cost_usd=cost)

    repo = state.get("repo", {})
    pr = state.get("pr", {})
    if repo.get("provider") == "github" and pr.get("number"):
        try:
            await gh.post_pr_comment(repo["full_name"], pr["number"], gh.render_comment(final))
        except Exception as e:      # noqa: BLE001
            logger.warning("publish.comment_failed", error=str(e))

    logger.info("review.published", task_id=state["task_id"], mergeable=summary["mergeable"])
    return {"final": final, "steps": state.get("steps", 0) + 1}
