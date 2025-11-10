from __future__ import annotations

from typing import Any, Dict, List
import logging
from pathlib import Path
import re

from app.services.chroma_service import ChromaClientProvider
from app.core.config import settings


_provider: ChromaClientProvider | None = None
LOG = logging.getLogger(__name__)


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
    base_name = _proto_collection_name(os_name)
    collection = provider.get_or_create_collection(base_name)

    # Compute final collection name the provider will use (suffixed by embedding id)
    try:
        embed_id = getattr(provider.embedding_fn, "name", lambda: "unknown")()
        suffix = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(embed_id)).strip("_")
        final_name = f"{base_name}__{suffix}" if suffix else base_name
    except Exception:
        embed_id = "unknown"
        final_name = base_name

    # Resolve Chroma storage location
    chroma_mode = getattr(settings, "CHROMA_MODE", "local")
    try:
        if str(chroma_mode).lower() == "http":
            chroma_location = f"http://{getattr(settings, 'CHROMA_SERVER_HOST', 'localhost')}:{getattr(settings, 'CHROMA_SERVER_PORT', 8000)}"
        else:
            chroma_location = str(Path(getattr(settings, "CHROMA_PERSIST_DIRECTORY", ".chroma")).resolve())
    except Exception:
        chroma_location = "-"

    # Attempt to get item count for visibility
    try:
        try:
            count = collection.count()  # type: ignore[attr-defined]
            count_val = int(count) if isinstance(count, int) else "unknown"
        except Exception:
            peek = collection.get(limit=1) or {}
            ids0 = peek.get("ids") or []
            count_val = 0 if not ids0 else "unknown"
    except Exception:
        count_val = "unknown"

    LOG.info(
        "prototype_router: using proto collection name=%s count=%s provider=%s chroma_mode=%s chroma=%s",
        final_name,
        count_val,
        embed_id,
        chroma_mode,
        chroma_location,
    )
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


