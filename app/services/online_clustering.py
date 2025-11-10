from __future__ import annotations

import uuid
import asyncio
import logging

from app.services.prototype_router import nearest_prototype
LOG = logging.getLogger(__name__)
from app.services.chroma_service import ChromaClientProvider, collection_name_for_os
from app.core.config import settings
from app.core.runtime_state import is_shutting_down


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


def assign_or_create_cluster(
    os_name: str,
    templated: str,
    *,
    raw_log: str | None = None,
    threshold: float | None = None
) -> str:
    """Assign log to nearest prototype within threshold or create a new cluster.

    When using LogBERT (LOGBERT_USE_RAW_LOGS=true), raw_log is embedded for semantic clustering.
    Otherwise, templated text is used for syntactic clustering.

    Returns the cluster_id (prototype id).
    """
    thresh = threshold if threshold is not None else settings.ONLINE_CLUSTER_DISTANCE_THRESHOLD

    # Decide what to embed: raw log (semantic) or templated (syntactic)
    use_raw = settings.EMBEDDING_PROVIDER.lower() == "logbert" and settings.LOGBERT_USE_RAW_LOGS
    text_to_embed = raw_log if (use_raw and raw_log) else templated

    # Log what we're processing (truncated for privacy)
    log_preview = text_to_embed[:100] + "..." if len(text_to_embed or "") > 100 else text_to_embed
    LOG.debug("online clustering: processing log os=%s use_raw=%s thresh=%.3f text='%s'",
             os_name, use_raw, thresh, log_preview)

    try:
        nearest = nearest_prototype(os_name, text_to_embed, k=1)
        LOG.debug("online clustering: found %d nearest prototypes", len(nearest))
    except Exception as e:
        LOG.warning("online clustering: error finding nearest prototypes: %s", e)
        nearest = []

    distance = None
    is_new_cluster = False

    if nearest:
        try:
            dist = nearest[0].get("distance")
            cid = str(nearest[0].get("id") or "")
            proto_text = nearest[0].get("document", "")[:100] + "..." if len(nearest[0].get("document", "")) > 100 else nearest[0].get("document", "")

            LOG.debug("online clustering: nearest prototype id=%s dist=%.4f proto_text='%s'",
                     cid, dist if dist is not None else -1, proto_text)
        except Exception as e:
            LOG.warning("online clustering: error extracting nearest prototype data: %s", e)
            dist = None
            cid = ""

        if isinstance(dist, (int, float)) and dist <= thresh and cid:
            distance = dist
            LOG.info("online clustering: ASSIGNED to existing cluster os=%s cluster=%s dist=%.4f thresh=%.3f",
                    os_name, cid, dist, thresh)
            # Record assignment metrics
            if settings.ENABLE_CLUSTER_METRICS:
                _record_online_metrics(os_name, cid, distance, False)
            return cid

        # Log rejection to aid diagnosis
        try:
            if isinstance(dist, (int, float)) and cid:
                LOG.info("online clustering: REJECTED nearest cluster os=%s cluster=%s dist=%.4f thresh=%.3f reason=distance_too_high",
                        os_name, cid, dist, thresh)
        except Exception:
            pass
    else:
        try:
            coll = _proto_collection_name(os_name)
        except Exception:
            coll = "unknown"
        LOG.debug(
            "online clustering: no existing prototypes found os=%s collection=%s text_len=%d",
            os_name,
            coll,
            len(text_to_embed or ""),
        )

    # Create a new prototype seeded with this log as its medoid/centroid
    cid = f"cluster_{uuid.uuid4().hex[:12]}"
    is_new_cluster = True
    distance = distance if distance is not None else 0.0

    # Include closest existing cluster (if any) and its distance in the creation log
    nearest_id = ""
    nearest_dist_str = "n/a"
    try:
        if nearest:
            _nid = str(nearest[0].get("id") or "")
            _nd = nearest[0].get("distance")
            nearest_id = _nid
            if isinstance(_nd, (int, float)):
                nearest_dist_str = f"{_nd:.4f}"
    except Exception:
        pass

    # If no existing prototypes were found, fall back to nearest template/log for observability
    nearest_tpl_id = ""
    nearest_tpl_dist_str = "n/a"
    if not nearest_id:
        try:
            provider = ChromaClientProvider()
            tcoll = provider.get_or_create_collection(collection_name_for_os(os_name))
            # Guard empty templates collection
            try:
                t_count = tcoll.count()  # type: ignore[attr-defined]
                t_empty = isinstance(t_count, int) and t_count == 0
            except Exception:
                tpeek = tcoll.get(limit=1) or {}
                t_empty = not (tpeek.get("ids") or [])
            if not t_empty:
                q = tcoll.query(query_texts=[text_to_embed], n_results=1, include=["ids", "distances"]) or {}
                ids0 = (q.get("ids") or [[]])[0]
                dists0 = (q.get("distances") or [[]])[0]
                if ids0:
                    nearest_tpl_id = ids0[0]
                    if dists0 and isinstance(dists0[0], (int, float)):
                        nearest_tpl_dist_str = f"{dists0[0]:.4f}"
            # If templates are empty or no neighbor found, try logs_<os>
            if not nearest_tpl_id:
                lcoll_name = f"{settings.CHROMA_LOG_COLLECTION_PREFIX}{_suffix_for_os(os_name)}"
                lcoll = provider.get_or_create_collection(lcoll_name)
                try:
                    l_count = lcoll.count()  # type: ignore[attr-defined]
                    l_empty = isinstance(l_count, int) and l_count == 0
                except Exception:
                    lpeek = lcoll.get(limit=1) or {}
                    l_empty = not (lpeek.get("ids") or [])
                if not l_empty:
                    q2 = lcoll.query(query_texts=[text_to_embed], n_results=1, include=["ids", "distances"]) or {}
                    ids1 = (q2.get("ids") or [[]])[0]
                    dists1 = (q2.get("distances") or [[]])[0]
                    if ids1:
                        nearest_tpl_id = ids1[0]
                        if dists1 and isinstance(dists1[0], (int, float)):
                            nearest_tpl_dist_str = f"{dists1[0]:.4f}"
        except Exception:
            pass

    LOG.info(
        "online clustering: CREATED new cluster os=%s cluster=%s nearest_cluster=%s nearest_dist=%s nearest_template=%s nearest_template_dist=%s thresh=%.3f",
        os_name,
        cid,
        nearest_id or "none",
        nearest_dist_str,
        nearest_tpl_id or "none",
        nearest_tpl_dist_str,
        thresh,
    )
    
    try:
        provider = ChromaClientProvider()
        base_name = _proto_collection_name(os_name)
        collection = provider.get_or_create_collection(base_name)
        collection.add(
            ids=[cid],
            documents=[text_to_embed],
            metadatas=[{
                "os": os_name,
                "label": "unknown",
                "rationale": "online",
                "size": 1,
                "exemplar_count": 0,
                "created_by": "online",
                "embedding_mode": "raw" if use_raw else "templated",
            }],
        )
    except Exception:
        # Print full stack for any persistence failure
        try:
            LOG.exception("online clustering: failed to persist prototype os=%s cluster=%s", os_name, cid)
        except Exception:
            pass
        # Best-effort; if storage fails we still return the id for downstream tagging
        pass
    
    # Record new cluster creation
    if settings.ENABLE_CLUSTER_METRICS:
        _record_online_metrics(os_name, cid, distance, is_new_cluster)
    
    return cid


def _record_online_metrics(os_name: str, cluster_id: str, distance: float, is_new_cluster: bool) -> None:
    """Record online clustering metrics asynchronously."""
    try:
        # Skip during shutdown to avoid creating tasks when loops are tearing down
        if is_shutting_down:
            return
        import redis.asyncio as aioredis
        from app.services.cluster_metrics import ClusterMetricsTracker
        
        async def _record():
            redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            tracker = ClusterMetricsTracker(redis_client)
            await tracker.record_online_cluster_assignment(os_name, cluster_id, distance, is_new_cluster)
            await redis_client.close()
        
        # Offload to a daemon thread running its own event loop to avoid
        # both "no running event loop" and pending task destruction on shutdown.
        import threading
        threading.Thread(target=lambda: asyncio.run(_record()), daemon=True).start()
    except Exception:
        pass  # Don't fail clustering if metrics fail






