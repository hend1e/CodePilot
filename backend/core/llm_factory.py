# backend/core/llm_factory.py
# LLM 工厂：全项目唯一取模型入口。按 Agent 类型路由到不同模型，统一超时/重试/预算口径。
# mock 模式下 get_llm() 返回 None，调用方必须走「确定性规则引擎 + 降级」路径。

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from backend.config import get_settings
from backend.core.exceptions import LLMError
from backend.core.logger import get_logger

logger = get_logger(__name__)

_HTTP_ASYNC = httpx.AsyncClient(
    trust_env=False, timeout=httpx.Timeout(120.0, connect=15.0)
)
_HTTP_SYNC = httpx.Client(trust_env=False, timeout=httpx.Timeout(120.0, connect=15.0))

# Agent 类型 → 配置槽位。槽位留空时回落到 chat / code，所以只配两个模型也能跑。
_AGENT_SLOT: dict[str, str] = {
    "review_security": "review",
    "review_performance": "review",
    "review_style": "review",
    "review_test": "review",
    "testgen": "testgen",
    "triage": "triage",
    "rag_answer": "rag",
    "rag_hyde": "rag",
    "router": "router",
    "summarize": "chat",
}

# 槽位 → 兜底槽位（code 类任务优先用代码模型）
_SLOT_FALLBACK: dict[str, str] = {
    "review": "code",
    "testgen": "code",
    "triage": "chat",
    "rag": "chat",
    "router": "chat",
    "chat": "chat",
}


def resolve_model(agent_type: str) -> str:
    """解析某个 Agent 实际使用的模型名（按需覆盖，未配置则回落）。"""
    if agent_type not in _AGENT_SLOT:
        raise LLMError(f"未知 agent_type: {agent_type}",
                       details={"available": list(_AGENT_SLOT)})
    s = get_settings()
    slot = _AGENT_SLOT[agent_type]
    explicit = getattr(s, f"llm_model_{slot}", "") or ""
    if explicit:
        return explicit
    fallback = _SLOT_FALLBACK.get(slot, "chat")
    return s.llm_model_code if fallback == "code" else s.llm_model_chat


class LLMFactory:
    _instances: dict[str, BaseChatModel] = {}

    @classmethod
    def _build_kwargs(cls, model: str, temperature: float, streaming: bool) -> dict:
        s = get_settings()
        return {
            "model": model,
            "model_provider": "openai",     # DeepSeek / 兼容网关统一走 OpenAI 协议
            "temperature": temperature,
            "api_key": s.llm_api_key,
            "base_url": s.llm_base_url,
            "max_retries": 0,               # 重试统一由 core/retry.py 负责
            "timeout": s.llm_timeout_seconds,
            "http_async_client": _HTTP_ASYNC,
            "http_client": _HTTP_SYNC,
            "streaming": streaming,
        }

    @classmethod
    def get_llm(cls, agent_type: str, temperature: float = 0.0,
                streaming: bool = False) -> BaseChatModel | None:
        s = get_settings()
        if not s.llm_enabled:
            return None
        model = resolve_model(agent_type)
        key = f"{model}|{temperature}|{streaming}"
        if key not in cls._instances:
            cls._instances[key] = init_chat_model(**cls._build_kwargs(model, temperature, streaming))
            logger.info("llm.model_initialized", agent_type=agent_type,
                        slot=_AGENT_SLOT[agent_type], model=model,
                        temperature=temperature, streaming=streaming)
        return cls._instances[key]

    @classmethod
    def get_structured_llm(cls, agent_type: str, schema, temperature: float = 0.0) -> Runnable | None:
        llm = cls.get_llm(agent_type, temperature=temperature)
        if llm is None:
            return None
        return llm.with_structured_output(schema, method="function_calling")

    @classmethod
    def clear_cache(cls) -> None:
        cls._instances.clear()


def get_llm(agent_type: str, temperature: float = 0.0,
            streaming: bool = False) -> BaseChatModel | None:
    return LLMFactory.get_llm(agent_type, temperature=temperature, streaming=streaming)


def get_structured_llm(agent_type: str, schema, temperature: float = 0.0) -> Runnable | None:
    return LLMFactory.get_structured_llm(agent_type, schema, temperature=temperature)


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """价格优先用内置表，未收录的模型用 .env 里的 LLM_PRICE_*_PER_M。"""
    s = get_settings()
    pin, pout = _PRICING.get(model, (s.llm_price_input_per_m, s.llm_price_output_per_m))
    return round(prompt_tokens / 1e6 * pin + completion_tokens / 1e6 * pout, 6)
