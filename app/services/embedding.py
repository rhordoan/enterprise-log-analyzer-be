from __future__ import annotations

from typing import Iterable, List
import json
import urllib.request
import urllib.error
import logging

import numpy as np
import ollama
from openai import OpenAI
from sentence_transformers import SentenceTransformer
from app.core.config import settings


class SentenceTransformerEmbeddingFunction:
    """Adapter for SentenceTransformer to be used with Chroma as embedding_function.

    The class is callable and returns a list of embeddings for a list of texts.
    """

    def __init__(self, model_name: str) -> None:
        self.model = SentenceTransformer(model_name)

    def __call__(self, input: Iterable[str]) -> List[List[float]]:
        embeddings = self.model.encode(list(input), normalize_embeddings=True)
        if isinstance(embeddings, np.ndarray):
            return embeddings.tolist()
        return [list(vec) for vec in embeddings]

    # Chroma may call .name() to verify embedding function identity
    def name(self) -> str:  # pragma: no cover - simple getter
        return f"sentence-transformers::{self.model.get_sentence_embedding_dimension()}"

    # Some Chroma paths call embed_documents/embed_query when available
    def embed_documents(self, texts: Iterable[str]) -> List[List[float]]:
        return self(list(texts))

    def embed_query(self, text: str) -> List[float]:
        result = self([text])
        return result[0] if result else []


def embed_single_text(embedding_function: SentenceTransformerEmbeddingFunction, text: str) -> List[float]:
    return embedding_function([text])[0]


class OpenAIEmbeddingFunction:
    """OpenAI embeddings adapter compatible with Chroma's embedding_function interface."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        # OpenAI Python SDK v1 uses client with api_key from env or provided
        self.client = OpenAI(
            api_key=api_key or settings.OPENAI_API_KEY,
            organization=settings.OPENAI_ORG_ID,
            project=settings.OPENAI_PROJECT,
        )
        self.model = model

    def __call__(self, input: Iterable[str]) -> List[List[float]]:
        inputs = list(input)
        if not inputs:
            return []
        # Batching can be added if needed; for simplicity, do one request
        response = self.client.embeddings.create(model=self.model, input=inputs)
        # Ensure ordering is preserved
        return [emb.embedding for emb in response.data]

    def name(self) -> str:  # pragma: no cover - simple getter
        return f"openai::{self.model}"

    # Chroma compatibility helpers
    def embed_documents(self, texts: Iterable[str]) -> List[List[float]]:
        return self(list(texts))

    def embed_query(self, text: str) -> List[float]:
        result = self([text])
        return result[0] if result else []


class OllamaEmbeddingFunction:
    """Ollama embeddings adapter using the official ollama python library.

    This implementation performs one request per input string for simplicity
    and robustness. It returns lists of floats compatible with Chroma's
    embedding_function interface.
    """

    # Module-level throttling for readiness logs to avoid spamming
    _logged_ready_keys: set[tuple[str, str]] = set()
    _last_error_ts: dict[tuple[str, str], float] = {}

    def __init__(self, base_url: str, model: str) -> None:
        self.client = ollama.Client(host=base_url)
        self.model = model
        logger = logging.getLogger(__name__)
        key = (base_url, model)
        # One-time readiness probe per (base_url, model)
        try:
            info = self.client.list()
            if key not in OllamaEmbeddingFunction._logged_ready_keys:
                num_models = len((info or {}).get("models", []))
                logger.info("ollama embedding provider ready host=%s model=%s models=%d", base_url, model, num_models)
                OllamaEmbeddingFunction._logged_ready_keys.add(key)
            else:
                # Subsequent initializations are quiet
                logger.debug("ollama embedding provider already initialized host=%s model=%s", base_url, model)
        except Exception as e:  # pragma: no cover - network
            # Rate-limit error logs to once per 60s per key
            import time as _time
            now = _time.time()
            last = OllamaEmbeddingFunction._last_error_ts.get(key, 0.0)
            if now - last >= 60.0:
                logger.warning("ollama embedding provider not reachable host=%s model=%s err=%s", base_url, model, e)
                OllamaEmbeddingFunction._last_error_ts[key] = now

    def __call__(self, input: Iterable[str]) -> List[List[float]]:
        texts = list(input)
        if not texts:
            return []

        embeddings: List[List[float]] = []
        for text in texts:
            try:
                response = self.client.embeddings(model=self.model, prompt=text)
                embedding = response.get("embedding")
                if not isinstance(embedding, list):
                     raise RuntimeError("ollama embeddings response missing 'embedding' list")
                embeddings.append(embedding)
            except ollama.ResponseError as e:  # pragma: no cover - network
                raise RuntimeError(f"ollama embeddings API error: {e.error}") from e
            except Exception as e: # pragma: no cover - unexpected
                raise RuntimeError(f"An unexpected error occurred with ollama embeddings: {e}") from e

        return embeddings

    def name(self) -> str:  # pragma: no cover - simple getter
        return f"ollama::{self.model}"

    # Chroma compatibility helpers
    def embed_documents(self, texts: Iterable[str]) -> List[List[float]]:
        return self(list(texts))

    def embed_query(self, text: str) -> List[float]:
        result = self([text])
        return result[0] if result else []
