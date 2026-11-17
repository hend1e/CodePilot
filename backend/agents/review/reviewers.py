# backend/agents/review/reviewers.py
# 单维度审查器：规则引擎 + LLM 的结构化输出，超时/降级隔离在单维度内。
# 关键点：LLM 失败不抛异常，转为 dim_status=degraded/failed + open_questions，主图继续。

import asyncio

from langchain_core.messages import HumanMessage, SystemMessage

from backend.agents.review.diff import DiffBundle
from backend.agents.review.prompts import build_review_prompt
from backend.agents.review.rules import run_rules, validate_evidence
from backend.agents.review.state import Finding, FindingList
from backend.config import get_settings
from backend.core.exceptions import BudgetExceeded, CodePilotError, LLMOutputInvalid
from backend.core.llm_factory import get_structured_llm
from backend.core.logger import get_logger
from backend.core.retry import estimate_tokens, with_retry

logger = get_logger(__name__)


class DimensionReviewer:
    def __init__(self, dimension: str):
        self.dimension = dimension
        self.timeout = get_settings().dim_timeouts[dimension]

    async def run(self, bundle: DiffBundle, repo_ctx: str, pr_meta: dict,
                  budget: dict) -> tuple[list[Finding], str, dict]:
        """返回 (findings, status, usage)。status: ok | degraded:llm | failed:xxx"""
        usage = {"prompt_tokens": 0, "completion_tokens": 0}

        # ① 规则引擎：永远先跑，即使 LLM 挂了也有产出
        rule_findings = run_rules(bundle, [self.dimension])
        added = bundle.added_line_index()
        rule_findings, _ = validate_evidence(rule_findings, added)

        # ② 预算检查：超预算直接降级为规则结果
        if budget.get("tokens_used", 0) >= budget.get("max_tokens", 10**9):
            return rule_findings, "degraded:budget", usage

        llm = get_structured_llm(f"review_{self.dimension}", FindingList)
        if llm is None:
            return rule_findings, "degraded:llm_disabled", usage

        rule_brief = "\n".join(
            f"- [{f.severity}] {f.file_path}:{f.line_start} {f.message}" for f in rule_findings
        )
        messages = build_review_prompt(
            self.dimension,
            bundle.text(max_chars=60_000),
            repo_ctx[:8000],
            rule_brief,
            pr_meta,
        )
        lc_messages = [
            SystemMessage(content=messages[0]["content"]),
            HumanMessage(content=messages[1]["content"]),
        ]
        usage["prompt_tokens"] = estimate_tokens(lc_messages[0].content + lc_messages[1].content)

        try:
            async with asyncio.timeout(self.timeout):
                result: FindingList = await with_retry(
                    lambda: llm.ainvoke(lc_messages), attempts=2, label=f"review.{self.dimension}"
                )
            if result is None:
                raise LLMOutputInvalid("结构化输出为空")
            llm_findings = [f for f in result.findings if f.dimension == self.dimension]
            llm_findings, dropped = validate_evidence(llm_findings, added)
            for f in llm_findings:
                f.source = "llm"
            if dropped:
                logger.info("review.evidence_dropped", dimension=self.dimension, dropped=dropped)
            usage["completion_tokens"] = estimate_tokens(
                "".join(f.message for f in llm_findings)
            )
            return rule_findings + llm_findings, "ok", usage

        except (asyncio.TimeoutError, LLMOutputInvalid) as e:
            logger.warning("review.degraded", dimension=self.dimension, error=type(e).__name__)
            return rule_findings, f"failed:{type(e).__name__}", usage
        except CodePilotError as e:
            logger.warning("review.degraded", dimension=self.dimension, error=e.code)
            return rule_findings, f"degraded:{e.code}", usage
        except BudgetExceeded:
            return rule_findings, "degraded:budget", usage
        except Exception as e:      # noqa: BLE001 —— 单维度失败绝不影响其他维度
            logger.error("review.unexpected", dimension=self.dimension, error=str(e), exc_info=True)
            return rule_findings, f"failed:{type(e).__name__}", usage


class RuleOnlyReviewer(DimensionReviewer):
    """mock 模式：只跑规则，便于 CI 与离线演示。"""

    async def run(self, bundle: DiffBundle, repo_ctx: str, pr_meta: dict,
                  budget: dict) -> tuple[list[Finding], str, dict]:
        added = bundle.added_line_index()
        findings, _ = validate_evidence(run_rules(bundle, [self.dimension]), added)
        return findings, "ok:rules_only", {"prompt_tokens": 0, "completion_tokens": 0}


def build_reviewer(dimension: str) -> DimensionReviewer:
    return DimensionReviewer(dimension) if get_settings().llm_enabled else RuleOnlyReviewer(dimension)
