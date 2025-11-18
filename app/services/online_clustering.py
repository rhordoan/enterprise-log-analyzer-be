from __future__ import annotations

import uuid
import asyncio
import logging

from app.services.prototype_router import nearest_prototype
from app.services.chroma_service import ChromaClientProvider
from app.core.config import settings

LOG = logging.getLogger(__name__)

_provider: ChromaClientProvider | None = None


def _get_provider() -> ChromaClientProvider:
    global _provider
    if _provider is None:
        _provider = ChromaClientProvider()
    return _provider


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
    except Exception as exc:
        LOG.warning("online clustering: prototype lookup failed os=%s err=%s", os_name, exc)
        nearest = []

    distance = None
    is_new_cluster = False
    
    if nearest:
        try:
            dist = nearest[0].get("distance")
            cid = str(nearest[0].get("id") or "")
        except Exception:
            dist = None
            cid = ""
        if isinstance(dist, (int, float)) and dist <= thresh and cid:
            distance = dist
            # Record assignment metrics
            if settings.ENABLE_CLUSTER_METRICS:
                _record_online_metrics(os_name, cid, distance, False)
            return cid

    # Create a new prototype seeded with this templated line as its medoid/centroid
    cid = f"cluster_{uuid.uuid4().hex[:12]}"
    is_new_cluster = True
    distance = distance if distance is not None else 0.0
    
    collection_name = _proto_collection_name(os_name)
    text_len = len(templated or "")
    try:
        provider = _get_provider()
        collection = provider.get_or_create_collection(collection_name)
        existing = -1
        try:
            cnt = collection.count()  # type: ignore[attr-defined]
            existing = int(cnt) if isinstance(cnt, int) else -1
        except Exception:
            pass
        LOG.debug(
            "online clustering: persisting prototype os=%s cluster=%s collection=%s text_len=%d existing=%d",
            os_name,
            cid,
            collection_name,
            text_len,
            existing,
        )
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
    except Exception as exc:
        LOG.exception(
            "online clustering: failed to persist prototype os=%s cluster=%s collection=%s text_len=%d",
            os_name,
            cid,
            collection_name,
            text_len,
        )
        try:
            import logging as _logging
            _logging.getLogger("app.kaboom").info(
                "persist_prototype_failed os=%s cluster=%s collection=%s err=%s",
                os_name,
                cid,
                collection_name,
                exc,
            )
        except Exception:
            pass
    
    # Record new cluster creation
    if settings.ENABLE_CLUSTER_METRICS:
        _record_online_metrics(os_name, cid, distance, is_new_cluster)
    
    return cid


def _record_online_metrics(os_name: str, cluster_id: str, distance: float, is_new_cluster: bool) -> None:
    """Record online clustering metrics asynchronously."""
    try:
        import redis.asyncio as aioredis
        from app.services.cluster_metrics import ClusterMetricsTracker
        
        async def _record():
            redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            tracker = ClusterMetricsTracker(redis_client)
            await tracker.record_online_cluster_assignment(os_name, cluster_id, distance, is_new_cluster)
            await redis_client.close()
        
        # Run in new event loop if needed
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(_record())
            else:
                loop.run_until_complete(_record())
        except RuntimeError:
            asyncio.run(_record())
    except Exception:
        pass  # Don't fail clustering if metrics fail






