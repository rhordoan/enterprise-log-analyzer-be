from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import math
import logging
import asyncio

from app.core.config import settings
from app.core.runtime_state import is_shutting_down
from app.services.chroma_service import ChromaClientProvider
from app.services.failure_rules import match_failure_signals

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


def _templates_collection_name(os_name: str) -> str:
    return f"{settings.CHROMA_COLLECTION_PREFIX}{_suffix_for_os(os_name)}"


def _logs_collection_name(os_name: str) -> str:
    return f"{settings.CHROMA_LOG_COLLECTION_PREFIX}{_suffix_for_os(os_name)}"


def _proto_collection_name(os_name: str) -> str:
    return f"{settings.CHROMA_PROTO_COLLECTION_PREFIX}{_suffix_for_os(os_name)}"


def _l2_norm(vec: List[float]) -> float:
    return math.sqrt(sum(v * v for v in vec)) or 1.0


def _normalize(vec: List[float]) -> List[float]:
    n = _l2_norm(vec)
    return [v / n for v in vec]


def _cosine_distance(a: List[float], b: List[float]) -> float:
    # expects normalized vectors
    dot = sum(x * y for x, y in zip(a, b))
    # numeric stability on bounds
    dot = max(min(dot, 1.0), -1.0)
    return 1.0 - dot


def _mean(vectors: List[List[float]]) -> List[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            acc[i] += v[i]
    return [x / float(len(vectors)) for x in acc]


@dataclass
class Prototype:
    cluster_id: str
    centroid: List[float]
    label: str
    rationale: str
    size: int
    medoid_document: str
    exemplar_ids: List[str]


def _single_pass_cluster(
    embeddings: List[List[float]],
    threshold: float,
    min_size: int,
) -> Tuple[List[List[int]], List[List[float]]]:
    """Simple incremental clustering by cosine distance to current centroids.

    Returns (clusters_indices, centroids).
    """
    normalized = [_normalize(e) for e in embeddings]
    clusters: List[List[int]] = []
    centroids: List[List[float]] = []

    for idx, vec in enumerate(normalized):
        if not centroids:
            clusters.append([idx])
            centroids.append(vec[:])
            continue
        # find nearest centroid
        distances = [_cosine_distance(vec, c) for c in centroids]
        best_i = min(range(len(distances)), key=lambda i: distances[i])
        if distances[best_i] <= threshold:
            clusters[best_i].append(idx)
            # update centroid as mean of members (on normalized vectors), then renormalize
            members = [normalized[i] for i in clusters[best_i]]
            centroids[best_i] = _normalize(_mean(members))
        else:
            clusters.append([idx])
            centroids.append(vec[:])

    # filter small clusters
    filtered: List[List[int]] = []
    filtered_centroids: List[List[float]] = []
    for c, ctr in zip(clusters, centroids):
        if len(c) >= max(1, min_size):
            filtered.append(c)
            filtered_centroids.append(ctr)
    return filtered, filtered_centroids


def _medoid_index(indices: List[int], vectors: List[List[float]], centroid: List[float]) -> int:
    best_idx = indices[0]
    best_dist = 1e9
    for i in indices:
        d = _cosine_distance(vectors[i], centroid)
        if d < best_dist:
            best_dist = d
            best_idx = i
    return best_idx


def _label_cluster(documents: List[str]) -> Tuple[str, str]:
    # Try medoid document first, fallback to majority over all docs
    signals = [match_failure_signals(doc) for doc in documents]
    labels = [s.get("label") for s in signals if s.get("has_signal")]
    if labels:
        # majority vote
        counts: Dict[str, int] = {}
        for l in labels:
            counts[l] = counts.get(l, 0) + 1
        majority = max(counts.items(), key=lambda kv: kv[1])[0]
        return majority, "keyword_rules"
    return "unknown", "no_signal"


def build_prototypes(
    ids: List[str],
    documents: List[str],
    embeddings: List[List[float]],
    clusters: List[List[int]],
    centroids: List[List[float]],
) -> List[Prototype]:
    prototypes: List[Prototype] = []
    for ci, member_indices in enumerate(clusters):
        centroid = centroids[ci]
        medoid_local = _medoid_index(member_indices, [
            _normalize(e) for e in embeddings
        ], centroid)
        medoid_global = medoid_local
        medoid_doc = documents[medoid_global]
        label, rationale = _label_cluster([documents[i] for i in member_indices])
        proto = Prototype(
            cluster_id=f"cluster_{ci}",
            centroid=centroid,
            label=label,
            rationale=rationale,
            size=len(member_indices),
            medoid_document=medoid_doc,
            exemplar_ids=[ids[i] for i in member_indices[:5]],
        )
        prototypes.append(proto)
    return prototypes


def upsert_prototypes(os_name: str, provider: ChromaClientProvider, prototypes: List[Prototype]) -> int:
    coll_name = _proto_collection_name(os_name)
    collection = provider.get_or_create_collection(coll_name)
    if not prototypes:
        return 0
    ids = [p.cluster_id for p in prototypes]
    docs = [p.medoid_document for p in prototypes]
    metas: List[Dict[str, Any]] = [
        {
            "os": os_name,
            "label": p.label,
            "rationale": p.rationale,
            "size": p.size,
            "exemplars": p.exemplar_ids,
        }
        for p in prototypes
    ]
    embeddings = [p.centroid for p in prototypes]
    try:
        collection.upsert(ids=ids, documents=docs, embeddings=embeddings, metadatas=metas)
    except Exception:
        # Print full stack with brief context to aid debugging
        dim = len(embeddings[0]) if embeddings and embeddings[0] else 0
        LOG.exception(
            "upsert_prototypes failed os=%s collection=%s count=%d dim=%d",
            os_name,
            coll_name,
            len(ids),
            dim,
        )
        raise
    return len(prototypes)


def cluster_os(
    os_name: str,
    *,
    include_logs_samples: int = 0,
    threshold: float | None = None,
    min_size: int | None = None,
) -> Dict[str, Any]:
    """Cluster templates (and optional sample of logs) to build prototypes for an OS."""
    provider = ChromaClientProvider()
    threshold = threshold if threshold is not None else settings.CLUSTER_DISTANCE_THRESHOLD
    min_size = min_size if min_size is not None else settings.CLUSTER_MIN_SIZE

    # Load templates
    templates = provider.get_or_create_collection(_templates_collection_name(os_name))
    t_data = templates.get(include=["embeddings", "documents", "metadatas", "ids"]) or {}
    t_ids = t_data.get("ids", [])
    t_docs = t_data.get("documents", [])
    t_embs = t_data.get("embeddings", [])

    ids: List[str] = list(t_ids)
    docs: List[str] = list(t_docs)
    embs: List[List[float]] = list(t_embs)

    # Optionally sample from logs_<os>
    if include_logs_samples and include_logs_samples > 0:
        logs = provider.get_or_create_collection(_logs_collection_name(os_name))
        l_data = logs.get(include=["embeddings", "documents", "metadatas", "ids"], limit=int(include_logs_samples)) or {}
        ids.extend(l_data.get("ids", []))
        docs.extend(l_data.get("documents", []))
        embs.extend(l_data.get("embeddings", []))

    if not embs:
        return {"os": os_name, "clusters": 0, "prototypes": 0}

    clusters, centroids = _single_pass_cluster(embs, threshold=threshold, min_size=min_size)
    prototypes = build_prototypes(ids, docs, embs, clusters, centroids)
    count = upsert_prototypes(os_name, provider, prototypes)
    
    # Record clustering metrics if enabled
    if settings.ENABLE_CLUSTER_METRICS and clusters and embs and not is_shutting_down:
        try:
            import redis.asyncio as aioredis
            from app.services.cluster_metrics import ClusterMetricsTracker
            
            async def _record_metrics():
                redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
                tracker = ClusterMetricsTracker(redis_client)
                await tracker.record_batch_clustering_metrics(os_name, clusters, embs, threshold, min_size)
                await redis_client.close()
            
            # Offload to a daemon thread running its own event loop to avoid
            # both "no running event loop" and pending task destruction on shutdown.
            import threading
            threading.Thread(target=lambda: asyncio.run(_record_metrics()), daemon=True).start()
        except Exception:
            pass  # Don't fail clustering if metrics fail
    
    return {"os": os_name, "clusters": len(clusters), "prototypes": count}


