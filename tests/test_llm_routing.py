"""模型路由单测：一个 provider 下，按 Agent 槽位解析模型，未配置则回落。"""

import pytest

from backend.config import get_settings
from backend.core.exceptions import LLMError
from backend.core.llm_factory import resolve_model

SLOTS = ("llm_model_review", "llm_model_testgen", "llm_model_triage",
         "llm_model_rag", "llm_model_router")


def _clear_overrides(monkeypatch):
    s = get_settings()
    for name in SLOTS:
        monkeypatch.setattr(s, name, "")
    monkeypatch.setattr(s, "llm_model_chat", "chat-x")
    monkeypatch.setattr(s, "llm_model_code", "code-x")
    return s


def test_fallback_to_chat_or_code(monkeypatch):
    _clear_overrides(monkeypatch)
    # 代码类任务回落 code
    assert resolve_model("review_security") == "code-x"
    assert resolve_model("review_test") == "code-x"
    assert resolve_model("testgen") == "code-x"
    # 对话类任务回落 chat
    assert resolve_model("triage") == "chat-x"
    assert resolve_model("rag_answer") == "chat-x"
    assert resolve_model("router") == "chat-x"
    assert resolve_model("summarize") == "chat-x"


def test_per_agent_override(monkeypatch):
    s = _clear_overrides(monkeypatch)
    monkeypatch.setattr(s, "llm_model_review", "strong-32b")
    monkeypatch.setattr(s, "llm_model_triage", "small-7b")
    assert resolve_model("review_security") == "strong-32b"
    assert resolve_model("testgen") == "code-x"      # 未覆盖，继续回落
    assert resolve_model("triage") == "small-7b"
    assert s.llm_model_map["review"] == "strong-32b"


def test_unknown_agent_raises():
    with pytest.raises(LLMError):
        resolve_model("no_such_agent")


def test_provider_blank_means_mock():
    from backend.config import Settings

    assert Settings(llm_provider="").llm_provider == "mock"
    assert Settings(llm_provider="  DeepSeek ").llm_provider == "deepseek"


def test_build_kwargs_does_not_leak_provider():
    """provider 不是 OpenAI 协议字段，不能塞进 model_kwargs 发给网关。"""
    from backend.core.llm_factory import LLMFactory

    kwargs = LLMFactory._build_kwargs("deepseek-chat", 0, False)
    assert "model_kwargs" not in kwargs
    assert kwargs["model_provider"] == "openai"
