# backend/core/exceptions.py
# 统一异常体系。区分「可重试」与「不可重试」，图节点里所有异常都必须转成结构化状态，
# 不允许裸抛异常穿透整张图（见 docs/ARCHITECTURE.md 设计原则 3）。


class CodePilotError(Exception):
    """所有自定义异常基类。"""

    code: str = "internal_error"
    retryable: bool = False

    def __init__(self, message: str, *, agent: str = "", details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.agent = agent
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "agent": self.agent,
                "retryable": self.retryable, "details": self.details}


class LLMError(CodePilotError):
    code = "llm_error"
    retryable = True


class LLMOutputInvalid(CodePilotError):
    code = "llm_output_invalid"


class RetrievalError(CodePilotError):
    code = "retrieval_error"
    retryable = True


class MilvusUnavailable(RetrievalError):
    code = "milvus_unavailable"


class SandboxError(CodePilotError):
    code = "sandbox_error"


class SandboxTimeout(SandboxError):
    code = "sandbox_timeout"


class IngestError(CodePilotError):
    code = "ingest_error"


class ProviderError(CodePilotError):
    code = "provider_error"
    retryable = True


class AuthenticationError(CodePilotError):
    code = "authentication_error"


class AuthorizationError(CodePilotError):
    code = "authorization_error"


class InvalidInput(CodePilotError):
    code = "invalid_input"


class BudgetExceeded(CodePilotError):
    code = "budget_exceeded"
