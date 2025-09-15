from __future__ import annotations

from typing import Iterable, Optional

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection

from app.core.config import settings
from app.services.embedding import (
    SentenceTransformerEmbeddingFunction,
    OpenAIEmbeddingFunction,
)


class ChromaClientProvider:
    """Factory for Chroma client and collections."""

    def __init__(self, embedding_model_name: Optional[str] = None) -> None:
        # Choose embedding provider
        if settings.EMBEDDING_PROVIDER.lower() == "openai":
            self.embedding_fn = OpenAIEmbeddingFunction(
                model=settings.OPENAI_EMBEDDING_MODEL,
                api_key=settings.OPENAI_API_KEY,
            )
        else:
            self.embedding_fn = SentenceTransformerEmbeddingFunction(
                embedding_model_name or settings.EMBEDDING_MODEL_NAME
            )
        self._client = self._create_client()

    def _create_client(self) -> ClientAPI:
        if settings.CHROMA_MODE.lower() == "http":
            return chromadb.HttpClient(
                host=settings.CHROMA_SERVER_HOST,
                port=settings.CHROMA_SERVER_PORT,
            )
        # default to local persistent client
        return chromadb.PersistentClient(path=settings.CHROMA_PERSIST_DIRECTORY)

    @property
    def client(self) -> ClientAPI:
        return self._client

    def get_or_create_collection(self, name: str) -> Collection:
        return self.client.get_or_create_collection(
            name=name,
            embedding_function=self.embedding_fn,  # type: ignore[arg-type]
            metadata={"source": "enterprise-log-analyzer", "type": "template"},
        )


def collection_name_for_os(os_name: str) -> str:
    os_key = os_name.strip().lower()
    if os_key in {"mac", "macos", "osx"}:
        suffix = "macos"
    elif os_key in {"linux"}:
        suffix = "linux"
    elif os_key in {"windows", "win"}:
        suffix = "windows"
    else:
        suffix = os_key
    return f"{settings.CHROMA_COLLECTION_PREFIX}{suffix}"


