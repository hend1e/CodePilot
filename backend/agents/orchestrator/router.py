# backend/agents/orchestrator/router.py
# 统一入口的意图路由：规则前置拦截（零 token）→ LLM 分类（可选）→ 默认兜底 qa。
# 常见高频、格式固定的输入（PR 链接、堆栈）直接走规则，省 token 也更快。

import asyncio
import json
import re

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from backend.agents.rag.prompts import INTENT_SYSTEM
from backend.config import get_settings
from backend.core.llm_factory import get_structured_llm
from backend.core.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

PR_URL_RE = re.compile(r"https?://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+)")
STACK_RE = re.compile(r'(Traceback \(most recent call last\)|File "[^"]+", line \d+)')
REVIEW_WORDS = ("审查", "评审", "review", "看看这个 pr", "看下这个 pr", "diff", "改动")
TEST_WORDS = ("生成单测", "写测试", "补测试", "test case", "单元测试")
DEFECT_WORDS = ("报错", "异常", "堆栈", "traceback", "崩溃", "bug 定位", "定位缺陷", "超时")


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
    if STACK_RE.search(text) or any(w in text.lower() for w in DEFECT_WORDS):
        return IntentResult("defect", None, "命中堆栈/报错特征", 0.85, "rule")
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
        intent = out.intent if out.intent in ("review", "qa", "test", "defect") else "qa"
        return IntentResult(intent, out.repo or None, out.reason or "LLM 分类", 0.8, "llm")
    except Exception as e:      # noqa: BLE001
        logger.warning("router.degraded", error=str(e)[:200])
        return IntentResult("qa", None, "分类失败，兜底问答", 0.3, "default")
