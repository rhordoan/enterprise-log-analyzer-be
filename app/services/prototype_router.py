from __future__ import annotations

from typing import Any, Dict, List

from app.services.chroma_service import ChromaClientProvider
from app.core.config import settings


_provider: ChromaClientProvider | None = None


def _suffix_for_os(os_name: str) -> str:
    key = (os_name or "").strip().lower()
    if key in {"mac", "macos", "osx"}:
        return "macos"
    if key in {"linux"}:
        return "linux"
    if key in {"windows", "win"}:
        return "windows"
    return key or "unknown"


def _proto_collection_name(os_name: str) -> str:
    return f"{settings.CHROMA_PROTO_COLLECTION_PREFIX}{_suffix_for_os(os_name)}"


def _get_provider() -> ChromaClientProvider:
    global _provider
    if _provider is None:
        _provider = ChromaClientProvider()
    return _provider


def nearest_prototype(os_name: str, templated_text: str, k: int = 3) -> List[Dict[str, Any]]:
    """Return top-k nearest prototypes from proto_<os> with distances.

    Output per item: {id, document, distance, metadata}
    """
    provider = _get_provider()
    collection = provider.get_or_create_collection(_proto_collection_name(os_name))
    if not templated_text:
        return []
    result = collection.query(query_texts=[templated_text], n_results=max(1, k), include=["distances", "metadatas", "documents"])
    out: List[Dict[str, Any]] = []
    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    for i in range(len(ids)):
        out.append({
            "id": ids[i],
            "document": docs[i] if i < len(docs) else "",
            "distance": dists[i] if i < len(dists) else None,
            "metadata": metas[i] if i < len(metas) else {},
        })
    return out


