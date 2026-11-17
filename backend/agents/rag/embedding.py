# backend/agents/rag/embedding.py
# 向量化后端：hash（零依赖）/ local（BGE-M3）/ openai（兼容 /embeddings）。
# 统一出口 Embedder.embed()，切换后端只改 .env，不改调用方。

import asyncio
import hashlib
import math
import re

import httpx

from backend.config import get_settings
from backend.core.exceptions import RetrievalError
from backend.core.logger import get_logger

logger = get_logger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]|\d+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class BaseEmbedder:
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class HashEmbedder(BaseEmbedder):
    """哈希向量化：token + 字符 3-gram 映射到固定维度，再做 L2 归一化。
    不追求语义精度，但确定性、可离线、可单测 —— 让 CI 与演示不依赖模型下载。"""

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        toks = _tokens(text)
        grams = [text[i:i + 3] for i in range(0, max(0, len(text) - 2))]
        for tok, weight in [(t, 1.0) for t in toks] + [(g, 0.35) for g in grams]:
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t[:8000]) for t in texts]


class LocalEmbedder(BaseEmbedder):
    """BGE-M3（sentence-transformers）。首次调用加载模型，之后常驻。"""

    def __init__(self, model_name: str, dim: int):
        self.model_name = model_name
        self.dim = dim
        self._model = None
        self._lock = asyncio.Lock()

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device="cpu")
            self.dim = self._model.get_sentence_embedding_dimension()
            logger.info("embedding.model_loaded", model=self.model_name, dim=self.dim)
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with self._lock:
            model = await asyncio.to_thread(self._load)
        vecs = await asyncio.to_thread(
            model.encode, texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False
        )
        return [v.tolist() for v in vecs]


class OpenAIEmbedder(BaseEmbedder):
    def __init__(self, base_url: str, api_key: str, model: str, dim: int):
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
            )
            if resp.status_code >= 400:
                raise RetrievalError(f"embedding 接口失败: {resp.status_code} {resp.text[:200]}")
            data = resp.json()["data"]
            return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]


def get_embedder() -> BaseEmbedder:
    s = get_settings()
    backend = s.embedding_backend.lower()
    if backend == "local":
        return LocalEmbedder(s.embedding_model, s.embedding_dim)
    if backend == "openai":
        return OpenAIEmbedder(s.embedding_base_url, s.embedding_api_key,
                              s.embedding_model, s.embedding_dim)
    return HashEmbedder(s.embedding_dim)
