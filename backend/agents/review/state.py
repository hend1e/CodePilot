# backend/agents/review/state.py
# PR 审查子图的状态与数据模型。核心设计：state 只放决策必需的最小集；
# 所有并行写入走 reducer；reducer 必须幂等且排序稳定。

import hashlib
import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

Dimension = Literal["security", "performance", "style", "test"]
Severity = Literal["block", "warn", "info"]

DIMENSIONS: tuple[Dimension, ...] = ("security", "performance", "style", "test")
SEVERITY_ORDER: dict[str, int] = {"block": 0, "warn": 1, "info": 2}
SEVERITY_LABEL = {"block": "阻断", "warn": "警告", "info": "建议"}


class Finding(BaseModel):
    """一条审查结论。evidence 必填：没有证据的结论在聚合阶段被丢弃。"""

    dimension: Dimension
    severity: Severity
    file_path: str
    line_start: int = 0
    line_end: int = 0
    rule_id: str | None = None
    evidence: str = Field(min_length=1, max_length=1200)
    message: str = Field(min_length=1, max_length=600)
    suggestion: str = ""
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    source: Literal["rule", "llm"] = "rule"

    def fingerprint(self) -> str:
        """去重指纹：维度 + 文件 + 行 + 规则/消息前缀。"""
        raw = f"{self.dimension}|{self.file_path}|{self.line_start}|{self.rule_id or self.message[:40]}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class FindingList(BaseModel):
    """LLM 结构化输出的包装（顶层必须是对象，不能是裸列表）。"""

    findings: list[Finding] = Field(default_factory=list)


def merge_findings(old: list[Finding], new: list[Finding]) -> list[Finding]:
    """并行 fan-out 的唯一写入口：按指纹去重、冲突取高置信、按严重度稳定排序。"""
    bucket: dict[str, Finding] = {f.fingerprint(): f for f in old}
    for f in new:
        key = f.fingerprint()
        cur = bucket.get(key)
        if cur is None or f.confidence > cur.confidence:
            bucket[key] = f
    return sorted(bucket.values(), key=lambda x: (SEVERITY_ORDER[x.severity],
                                                  x.file_path, x.line_start, x.rule_id or ""))


def merge_degraded(old: bool, new: bool) -> bool:
    """降级标记是「或」语义：任一路降级即整体降级；并行写必须走 reducer。"""
    return bool(old or new)


def merge_status(old: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    """维度状态合并：后写覆盖（同一维度只可能有一个 reviewer 写）。"""
    return {**old, **new}


class TestReport(BaseModel):
    status: Literal["passed", "failed", "error", "skipped"] = "skipped"
    passed: int = 0
    failed: int = 0
    coverage: float | None = None
    stdout_tail: str = ""
    error: str | None = None
    duration_ms: int = 0


class HitlDecision(BaseModel):
    action: Literal["approve", "reject", "edit"]
    comment: str = ""
    edited_patch: str | None = None


class ReviewState(TypedDict, total=False):
    # ── 任务上下文 ──
    task_id: str
    tenant_id: str
    thread_id: str
    repo: dict                 # {id, full_name, local_path, provider}
    pr: dict                   # {number, title, base_sha, head_sha, html_url, author}
    diff_text: str             # 原始 unified diff
    stack_trace: str           # 缺陷定位输入
    diff: dict                 # DiffBundle 的 dict 形态（便于落库/调试）
    requested_dimensions: list[str]

    # ── 调度与检索 ──
    triage: dict               # {chain, pr_type, size, needs_ctx, confidence}
    repo_ctx: dict             # {chunks: [...], degraded: bool}

    # ── 并行审查结果（唯一并发写入口）──
    findings: Annotated[list[Finding], merge_findings]
    dim_status: Annotated[dict[str, str], merge_status]
    usage: Annotated[list[dict], operator.add]

    # ── 测试生成 / 沙箱 / 人工 ──
    test_patch: str | None
    test_targets: list[str]
    testgen_rounds: int
    sandbox_report: dict | None
    hitl: dict | None
    want_defect: bool

    # ── 预算与防死循环 ──
    budget: dict
    steps: int
    no_progress: int
    open_questions: list[str]

    # ── 结论 ──
    degraded: Annotated[bool, merge_degraded]
    final: dict | None


def summarize(findings: list[Finding]) -> dict:
    """聚合统计：给 API、评论、指标统一口径。"""
    counts = {"block": 0, "warn": 0, "info": 0}
    by_dim: dict[str, dict[str, int]] = {d: {"block": 0, "warn": 0, "info": 0} for d in DIMENSIONS}
    for f in findings:
        counts[f.severity] += 1
        by_dim[f.dimension][f.severity] += 1
    return {
        "total": len(findings),
        "by_severity": counts,
        "by_dimension": by_dim,
        "mergeable": counts["block"] == 0,
        "blocking": [f.model_dump() for f in findings if f.severity == "block"],
    }
