# backend/config.py
# 全项目唯一配置中心。所有模块只通过 get_settings() 取配置，禁止各自读 os.environ。

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """环境变量 / .env.local → 强类型配置对象。"""

    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── 应用 ──
    app_env: str = "local"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    default_tenant_id: str = "tenant_default"
    inline_worker: bool = True      # 单进程开发时内嵌队列消费者

    # ── PostgreSQL ──
    db_host: str = "localhost"
    db_port: int = 5434
    db_name: str = "codepilot"
    db_user: str = "codepilot"
    db_password: str = "codepilot_pwd"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def psycopg_dsn(self) -> str:
        """LangGraph AsyncPostgresSaver 用 libpq DSN（psycopg）。"""
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # ── Redis 队列 ──
    redis_url: str = "redis://localhost:6381/0"
    queue_name: str = "codepilot:reviews"
    queue_group: str = "codepilot-workers"

    # ── Milvus ──
    milvus_host: str = "localhost"
    milvus_port: int = 19532
    milvus_collection: str = "codepilot_chunks"

    # ── LLM ──
    llm_provider: str = "deepseek"          # deepseek | openai | mock
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model_chat: str = "deepseek-chat"
    llm_model_code: str = "deepseek-chat"
    llm_timeout_seconds: float = 120.0

    # ── Embedding ──
    embedding_backend: str = "hash"         # hash | local | openai
    embedding_model: str = "BAAI/bge-m3"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_dim: int = 512

    # ── Rerank ──
    rerank_backend: str = "lexical"         # none | lexical | cross-encoder
    rerank_model: str = "BAAI/bge-reranker-large"
    rerank_top_k: int = 8
    retrieve_top_k: int = 20

    # ── 审查预算（防死循环 / 防成本失控）──
    review_dim_timeout_security: float = 90
    review_dim_timeout_performance: float = 75
    review_dim_timeout_style: float = 45
    review_dim_timeout_test: float = 45
    review_max_steps: int = 24
    review_max_tokens: int = 120_000
    review_max_cost_usd: float = 0.35

    # ── 沙箱 ──
    sandbox_mode: str = "docker"            # docker | local | off
    sandbox_image: str = "codepilot/sandbox:py311"
    sandbox_timeout_seconds: int = 180
    sandbox_memory: str = "512m"
    sandbox_cpus: str = "1.0"
    sandbox_pids_limit: int = 128
    sandbox_workspace_root: str = "./workspace"

    # ── 仓库接入 ──
    github_token: str = ""
    github_api_base: str = "https://api.github.com"
    github_webhook_secret: str = ""
    github_comment_enabled: bool = False
    repo_local_roots: str = "./workspace"

    @property
    def repo_local_root_list(self) -> list[str]:
        return [p.strip() for p in self.repo_local_roots.split(",") if p.strip()]

    # ── JWT ──
    jwt_secret_key: str = "change_me_in_production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 10080

    # ── 可观测（可选）──
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ── 派生属性 ──
    @property
    def llm_enabled(self) -> bool:
        """mock 模式：不调用任何大模型，只跑确定性规则引擎。"""
        return self.llm_provider.lower() != "mock" and bool(self.llm_api_key)

    @property
    def dim_timeouts(self) -> dict[str, float]:
        return {
            "security": self.review_dim_timeout_security,
            "performance": self.review_dim_timeout_performance,
            "style": self.review_dim_timeout_style,
            "test": self.review_dim_timeout_test,
        }

    @field_validator("app_env")
    @classmethod
    def _check_env(cls, v: str) -> str:
        return v.lower()

    def validate_production(self) -> list[str]:
        """生产启动前自检：返回缺失/危险的配置项列表（空列表 = 通过）。"""
        problems: list[str] = []
        if self.app_env == "prod":
            if self.llm_enabled and not self.llm_api_key:
                problems.append("LLM_API_KEY 未配置")
            if self.jwt_secret_key == "change_me_in_production":
                problems.append("JWT_SECRET_KEY 仍为默认值")
            if self.app_debug:
                problems.append("生产环境不应开启 APP_DEBUG")
            if not self.github_webhook_secret:
                problems.append("GITHUB_WEBHOOK_SECRET 未配置，webhook 无法验签")
        return problems


@lru_cache()
def get_settings() -> Settings:
    return Settings()
