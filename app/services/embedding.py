from __future__ import annotations

from typing import Iterable, List
import logging
import time

import numpy as np
import ollama
from openai import OpenAI
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer, AutoModel
import torch

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
    def embed_documents(self, input: Iterable[str]) -> List[List[float]]:
        return self(list(input))

    def embed_query(self, input: str) -> List[List[float]]:
        return self([input])


class LogBERTEmbeddingFunction:
    """LogBERT embeddings for semantic log understanding."""

    _logged_ready_keys: set[str] = set()

    @staticmethod
    def _has_meta_tensors(model: torch.nn.Module) -> bool:
        for tensor in model.parameters():
            if tensor.device.type == "meta":
                return True
        for tensor in model.buffers():
            if tensor.device.type == "meta":
                return True
        return False

    def __init__(self, model_name: str = "bert-base-uncased", device: str = "cpu") -> None:
        self.model_name = model_name
        logger = logging.getLogger(__name__)

        # Force CPU to ensure stability and avoid meta/cuda mismatch errors
        self.device = "cpu"

        try:
            # Minimal kwargs: removed device_map and low_cpu_mem_usage to prevent
            # 'accelerate' from intervening and leaving tensors on the 'meta' device.
            load_kwargs: dict[str, object] = {
                "trust_remote_code": True,
            }

            self.tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
            
            # Standard load. Without device_map, this loads directly to CPU RAM.
            self.model = AutoModel.from_pretrained(model_name, **load_kwargs)

            # Verify that weights are actually loaded (not meta)
            if LogBERTEmbeddingFunction._has_meta_tensors(self.model):
                logger.warning(
                    "LogBERT model loaded with meta tensors. Attempting to force materialization to CPU."
                )
                # Fallback: try to force move (though usually from_pretrained should have done this)
                # If this fails, the environment configuration for transformers/accelerate is likely forcing meta.
                self.model.to_empty(device="cpu") 
                # Note: to_empty initializes random weights; purely for structure. 
                # Ideally we want real weights. If we are here, the real weights failed to load.
                # Re-raising to alert the user is safer than using random weights.
                raise RuntimeError(
                    "LogBERT model loaded as meta tensors (empty weights). "
                    "This usually indicates an issue with 'accelerate' or 'device_map' settings in the environment."
                )

            self.model.eval()

            if model_name not in LogBERTEmbeddingFunction._logged_ready_keys:
                logger.info("logbert embedding provider ready model=%s device=%s", model_name, self.device)
                LogBERTEmbeddingFunction._logged_ready_keys.add(model_name)
        except Exception as e:
            logger.error("logbert embedding provider failed to initialize model=%s err=%s", model_name, e)
            raise

    def _mean_pooling(self, model_output: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        token_embeddings = model_output[0]
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
        return torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(
            input_mask_expanded.sum(1), min=1e-9
        )

    def __call__(self, input: Iterable[str]) -> List[List[float]]:
        # Chroma may pass in a variety of types here (str, list[str], tuples, etc.).
        # Coerce everything defensively to plain strings to avoid tokenizer
        # type errors like:
        # "TextEncodeInput must be Union[TextInputSequence, Tuple[InputSequence, InputSequence]]"
        raw_items = list(input)
        if not raw_items:
            return []

        texts: List[str] = []
        for value in raw_items:
            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, (list, tuple)):
                try:
                    texts.append(" ".join(map(str, value)))
                except Exception:
                    texts.append(str(value))
            elif value is None:
                texts.append("")
            else:
                texts.append(str(value))

        encoded = self.tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )

        # Ensure inputs are on the correct device (CPU)
        encoded = {k: v.to(self.device) for k, v in encoded.items()}

        with torch.no_grad():
            model_output = self.model(**encoded)

        embeddings = self._mean_pooling(model_output, encoded["attention_mask"])
        embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)

        result = embeddings.cpu().numpy().tolist()

        return result

    def name(self) -> str:
        return f"logbert::{self.model_name}"

    def embed_documents(self, input: Iterable[str]) -> List[List[float]]:
        return self(list(input))

    def embed_query(self, input: str) -> List[List[float]]:
        return self([input])


def embed_single_text(
    embedding_function: SentenceTransformerEmbeddingFunction, text: str
) -> List[List[float]]:
    """
    Correctly embeds a single text string and returns it in the expected
    List[List[float]] format for API compatibility.
    """
    return embedding_function([text])


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
    def embed_documents(self, input: Iterable[str]) -> List[List[float]]:
        return self(list(input))

    def embed_query(self, input: str) -> List[List[float]]:
        return self([input])


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
            now = time.time()
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
            # Defensively coerce any non-string input (e.g., ['text']) to a string
            coerced: str
            if isinstance(text, str):
                coerced = text
            elif isinstance(text, (list, tuple)):
                try:
                    coerced = " ".join(map(str, text))
                except Exception:
                    coerced = str(text)
            else:
                coerced = str(text)
            try:
                response = self.client.embeddings(model=self.model, prompt=coerced)
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
    def embed_documents(self, input: Iterable[str]) -> List[List[float]]:
        return self(list(input))

    def embed_query(self, input: str) -> List[List[float]]:
        return self([input])