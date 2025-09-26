from __future__ import annotations

from typing import Any
import uuid

from app.services.prototype_router import nearest_prototype
from app.services.chroma_service import ChromaClientProvider
from app.core.config import settings


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


def assign_or_create_cluster(os_name: str, templated: str, *, threshold: float | None = None) -> str:
    """Assign templated text to nearest prototype within threshold or create a new cluster.

    Returns the cluster_id (prototype id).
    """
    thresh = threshold if threshold is not None else settings.ONLINE_CLUSTER_DISTANCE_THRESHOLD

    try:
        nearest = nearest_prototype(os_name, templated, k=1)
    except Exception:
        nearest = []

    if nearest:
        try:
            dist = nearest[0].get("distance")
            cid = str(nearest[0].get("id") or "")
        except Exception:
            dist = None
            cid = ""
        if isinstance(dist, (int, float)) and dist <= thresh and cid:
            return cid

    # Create a new prototype seeded with this templated line as its medoid/centroid
    cid = f"cluster_{uuid.uuid4().hex[:12]}"
    try:
        provider = ChromaClientProvider()
        collection = provider.get_or_create_collection(_proto_collection_name(os_name))
        collection.add(
            ids=[cid],
            documents=[templated],
            metadatas=[{
                "os": os_name,
                "label": "unknown",
                "rationale": "online",
                "size": 1,
                "exemplars": [],
                "created_by": "online",
            }],
        )
    except Exception:
        # Best-effort; if storage fails we still return the id for downstream tagging
        pass
    return cid


