from __future__ import annotations

from typing import Iterable, List

import numpy as np
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


def embed_single_text(embedding_function: SentenceTransformerEmbeddingFunction, text: str) -> List[float]:
    return embedding_function([text])[0]


class OpenAIEmbeddingFunction:
    """OpenAI embeddings adapter compatible with Chroma's embedding_function interface."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        # OpenAI Python SDK v1 uses client with api_key from env or provided
        self.client = OpenAI(api_key=api_key or settings.OPENAI_API_KEY)
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


