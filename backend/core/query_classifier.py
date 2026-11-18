# backend/core/query_classifier.py
# 查询意图分类（对应课件 5.8）：决定要不要做 HyDE / 多 Query 改写。
# 规则优先（零 token），LLM 仅作可选补充 —— 分类错了最多多花一点检索成本，不影响正确性。

import re

from backend.agents.qa.prompts import QUERY_REWRITE_SYSTEM
from backend.config import get_settings
from backend.core.llm_factory import get_structured_llm
from backend.core.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

LOCATE_WORDS = ("在哪", "哪里", "哪个文件", "定位", "定义", "实现", "调用链", "谁调用",
                "where is", "which file", "implemented")
HOWTO_WORDS = ("怎么", "如何", "怎样", "流程", "步骤", "怎么用", "how to", "how do")
CONCEPT_WORDS = ("为什么", "原理", "区别", "是什么", "作用", "好处", "why", "what is")
_SYMBOL_RE = re.compile(r"`([A-Za-z_][\w.]{2,})`|\b([a-z]+_[a-z_]+)\b|\b([A-Z][a-zA-Z]{3,})\b")
_PATH_RE = re.compile(r"[\w./-]+\.(py|js|ts|java|go|md)\b")


class _QueryTypeOut(__import__("pydantic").BaseModel):
    kind: str = "general"
    reason: str = ""


def classify_question(question: str) -> dict:
    """返回 {kind, needs_hyde, needs_multi_query, symbols, reason}。

    kind:
      locate  —— 找具体代码位置（符号/文件/调用链）
      howto   —— 用法与流程
      concept —— 原理与设计取舍
      general —— 其他
    """
    q = question.strip()
    low = q.lower()
    symbols = [m.group(0) for m in _SYMBOL_RE.finditer(q) if m.group(0)]
    paths = _PATH_RE.findall(q)
    has_symbol = bool(symbols) or bool(paths)

    if has_symbol or any(w in low for w in LOCATE_WORDS):
        kind, reason = "locate", "包含符号/路径或定位词"
    elif any(w in low for w in HOWTO_WORDS):
        kind, reason = "howto", "包含用法/流程词"
    elif any(w in low for w in CONCEPT_WORDS):
        kind, reason = "concept", "包含原理/对比词"
    else:
        kind, reason = "general", "默认"

    # locate 不需要 HyDE（关键词已经很强）；concept 最适合 HyDE 补语义
    return {
        "kind": kind,
        "reason": reason,
        "needs_hyde": kind == "concept",
        "needs_multi_query": kind in ("concept", "howto"),
        "symbols": symbols[:5],
        "paths": paths[:5],
    }


async def llm_classify(question: str) -> dict | None:
    """可选：LLM 复核规则分类（失败返回 None，调用方沿用规则结果）。"""
    llm = get_structured_llm("rag_hyde", _QueryTypeOut)
    if llm is None:
        return None
    try:
        import asyncio

        async with asyncio.timeout(15):
            out = await llm.ainvoke([{"role": "system", "content": QUERY_REWRITE_SYSTEM},
                                     {"role": "user", "content": question}])
        if out.kind in ("locate", "howto", "concept", "general"):
            return {"kind": out.kind, "reason": out.reason or "LLM 分类",
                    "needs_hyde": out.kind == "concept",
                    "needs_multi_query": out.kind in ("concept", "howto")}
    except Exception as e:      # noqa: BLE001
        logger.warning("query_classifier.degraded", error=str(e)[:200])
    return None
