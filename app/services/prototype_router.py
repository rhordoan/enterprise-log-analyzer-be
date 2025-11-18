from __future__ import annotations

from typing import Any, Dict, List
import logging
from pathlib import Path
import re
import math

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

    # Attempt to get item count for visibility and to guard empty collections
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

    LOG.debug(
        "prototype_router: using proto collection name=%s count=%s provider=%s chroma_mode=%s chroma=%s",
        final_name,
        count_val,
        embed_id,
        chroma_mode,
        chroma_location,
    )
    # If collection appears empty, avoid querying the index to prevent
    # hnswlib \"index out of range in self\" errors.
    try:
        if isinstance(count_val, int) and count_val == 0:
            return []
    except Exception:
        # If we can't reliably read count, proceed but rely on downstream guards
        pass

    if not templated_text:
        return []
    try:
        result = collection.query(
            query_texts=[templated_text],
            n_results=max(1, k),
            include=["distances", "metadatas", "documents"],
        )
    except Exception as exc:
        # hnswlib raises "index out of range in self" when the index is empty even if
        # metadata rows already exist (frequent during cold starts before prototypes
        # are persisted). Treat this as "no prototypes yet" instead of surfacing an error.
        err_msg = str(exc).lower()
        if "index out of range in self" in err_msg or "the number of elements is zero" in err_msg:
            LOG.debug(
                "prototype_router: empty proto collection detected os=%s name=%s; returning no matches",
                os_name,
                final_name,
            )
            return []
        raise
    out: List[Dict[str, Any]] = []
    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    for i in range(len(ids)):
        # Sanitize distances: treat non-finite values as missing
        dist_val = dists[i] if i < len(dists) else None
        if not isinstance(dist_val, (int, float)) or not math.isfinite(dist_val):
            dist_val = None
        out.append({
            "id": ids[i],
            "document": docs[i] if i < len(docs) else "",
            "distance": dist_val,
            "metadata": metas[i] if i < len(metas) else {},
        })
    # If we didn't get any valid distances, fall back to explicit embedding query
    if out and not any(isinstance(item.get("distance"), (int, float)) for item in out):
        try:
            # Embed the query explicitly to avoid back-end issues returning NaN
            query_emb = provider.embedding_fn.embed_query(templated_text)
            result2 = collection.query(query_embeddings=query_emb, n_results=max(1, k), include=["distances", "metadatas", "documents"])
            out2: List[Dict[str, Any]] = []
            ids2 = (result2.get("ids") or [[]])[0]
            docs2 = (result2.get("documents") or [[]])[0]
            dists2 = (result2.get("distances") or [[]])[0]
            metas2 = (result2.get("metadatas") or [[]])[0]
            for i in range(len(ids2)):
                dist_val2 = dists2[i] if i < len(dists2) else None
                if not isinstance(dist_val2, (int, float)) or not math.isfinite(dist_val2):
                    dist_val2 = None
                out2.append({
                    "id": ids2[i],
                    "document": docs2[i] if i < len(docs2) else "",
                    "distance": dist_val2,
                    "metadata": metas2[i] if i < len(metas2) else {},
                })
            # Only return fallback if it provided results
            if out2:
                return out2
        except Exception:
            # Keep original output if fallback fails
            pass
    return out


