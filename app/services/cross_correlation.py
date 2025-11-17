from __future__ import annotations

from typing import Any, Dict, List, Tuple

import logging

from app.core.config import settings
from app.services.chroma_service import ChromaClientProvider
from app.services.clustering_service import _single_pass_cluster, _normalize, _cosine_distance
import numpy as np  # type: ignore
try:
    import hdbscan  # type: ignore
except Exception:  # pragma: no cover
    hdbscan = None  # will be validated at call site

LOG = logging.getLogger(__name__)


def _logs_collection_name(os_name: str) -> str:
    key = (os_name or "").strip().lower()
    if key in {"mac", "macos", "osx"}:
        key = "macos"
    elif key in {"windows", "win"}:
        key = "windows"
    elif key in {"linux"}:
        key = "linux"
    else:
        key = key or "unknown"
    return f"{settings.CHROMA_LOG_COLLECTION_PREFIX}{key}"


def _proto_collection_name(os_name: str) -> str:
    key = (os_name or "").strip().lower()
    if key in {"mac", "macos", "osx"}:
        key = "macos"
    elif key in {"windows", "win"}:
        key = "windows"
    elif key in {"linux"}:
        key = "linux"
    else:
        key = key or "unknown"
    return f"{settings.CHROMA_PROTO_COLLECTION_PREFIX}{key}"


def _compute_medoid_index(indices: List[int], vectors: List[List[float]], centroid: List[float]) -> int:
    best_idx = indices[0]
    best_dist = 1e9
    for i in indices:
        d = _cosine_distance(vectors[i], centroid)
        if d < best_dist:
            best_dist = d
            best_idx = i
    return best_idx


def compute_global_clusters(
    *,
    limit_per_source: int = 200,
    threshold: float | None = None,
    min_size: int | None = None,
    include_logs_per_cluster: int = 20,
) -> Dict[str, Any]:
    """Cluster logs across all OS collections to find cross-source correlations.

    Returns:
      {
        "params": {...},
        "clusters": [
          {
            "id": "gcluster_0",
            "size": 15,
            "centroid": [...],
            "medoid_document": "....",
            "source_breakdown": {"filetail:/var/log/syslog": 8, "snmp:...": 7},
            "os_breakdown": {"linux": 9, "macos": 3, "windows": 3},
            "sample_logs": [{ id, document, os, source, raw }]
          },
          ...
        ]
      }
    """
    thr = threshold if threshold is not None else settings.CLUSTER_DISTANCE_THRESHOLD
    ms = min_size if min_size is not None else settings.CLUSTER_MIN_SIZE

    provider = ChromaClientProvider()

    ids: List[str] = []
    docs: List[str] = []
    embs: List[List[float]] = []
    metas: List[Dict[str, Any]] = []

    # Collect from each OS logs collection, then enforce per-source cap
    for os_name in ("linux", "macos", "windows", "network"):
        try:
            coll = provider.get_or_create_collection(_logs_collection_name(os_name))
            # Note: 'ids' is not a valid value for 'include'; Chroma always returns ids alongside requested fields.
            data = coll.get(include=["embeddings", "documents", "metadatas"], limit=2000) or {}
        except Exception as exc:
            LOG.info("correlation: failed to read logs for os=%s err=%s", os_name, exc)
            data = {}
        os_ids: List[str] = list(data.get("ids", []))
        os_docs: List[str] = list(data.get("documents", []))
        os_embs: List[List[float]] = list(data.get("embeddings", []))
        os_metas_raw = data.get("metadatas", []) or []
        os_metas: List[Dict[str, Any]] = [m or {} for m in os_metas_raw]

        # Group by source and take up to limit_per_source
        by_source: Dict[str, List[int]] = {}
        for i in range(len(os_ids)):
            src = str((os_metas[i] or {}).get("source") or "")
            by_source.setdefault(src, []).append(i)
        for _, idxs in by_source.items():
            for i in idxs[: max(0, int(limit_per_source))]:
                ids.append(os_ids[i])
                docs.append(os_docs[i] if i < len(os_docs) else "")
                embs.append(os_embs[i] if i < len(os_embs) else [])
                meta = dict(os_metas[i] or {})
                if "os" not in meta:
                    meta["os"] = os_name
                metas.append(meta)

    if not embs:
        return {"params": {"threshold": thr, "min_size": ms, "limit_per_source": limit_per_source, "include_logs_per_cluster": include_logs_per_cluster}, "clusters": []}

    clusters, centroids = _single_pass_cluster(embs, threshold=thr, min_size=ms)

    # Normalize embeddings once for medoid computation
    normalized = [_normalize(v) for v in embs]

    out_clusters: List[Dict[str, Any]] = []
    for ci, member_indices in enumerate(clusters):
        centroid = centroids[ci]
        medoid_idx_local = _compute_medoid_index(member_indices, normalized, centroid)
        # medoid_idx_local is global since member_indices stores global indices
        medoid_global_idx = medoid_idx_local

        # Counts
        src_counts: Dict[str, int] = {}
        os_counts: Dict[str, int] = {}
        for gi in member_indices:
            meta = metas[gi] if gi < len(metas) else {}
            src = str((meta or {}).get("source") or "")
            osn = str((meta or {}).get("os") or "")
            src_counts[src] = src_counts.get(src, 0) + 1
            os_counts[osn] = os_counts.get(osn, 0) + 1

        # Sample logs
        samples: List[Dict[str, Any]] = []
        for gi in member_indices[: max(0, int(include_logs_per_cluster))]:
            meta = metas[gi] if gi < len(metas) else {}
            samples.append({
                "id": ids[gi] if gi < len(ids) else "",
                "document": docs[gi] if gi < len(docs) else "",
                "os": (meta or {}).get("os", ""),
                "source": (meta or {}).get("source", ""),
                "raw": (meta or {}).get("raw", ""),
            })

        out_clusters.append({
            "id": f"gcluster_{ci}",
            "size": len(member_indices),
            "centroid": centroid,
            "medoid_document": docs[medoid_global_idx] if medoid_global_idx < len(docs) else "",
            "source_breakdown": src_counts,
            "os_breakdown": os_counts,
            "sample_logs": samples,
        })

    result = {
        "params": {
            "threshold": thr,
            "min_size": ms,
            "limit_per_source": limit_per_source,
            "include_logs_per_cluster": include_logs_per_cluster,
        },
        "clusters": out_clusters,
    }
    return result


def build_graph_from_clusters(clusters_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Transform clusters response into a simple graph with source and cluster nodes."""
    clusters = clusters_payload.get("clusters", []) or []
    # Nodes: all sources + clusters
    source_nodes: Dict[str, Dict[str, Any]] = {}
    cluster_nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    for c in clusters:
        cid = str(c.get("id") or "")
        csize = int(c.get("size") or 0)
        cluster_nodes.append({"id": cid, "type": "cluster", "label": cid, "size": csize})
        sb = c.get("source_breakdown") or {}
        for src, cnt in sb.items():
            sid = f"source::{src}"
            if sid not in source_nodes:
                source_nodes[sid] = {"id": sid, "type": "source", "label": src or "unknown", "size": 1}
            edges.append({"source": sid, "target": cid, "weight": int(cnt or 0)})

    nodes = list(source_nodes.values()) + cluster_nodes
    return {"nodes": nodes, "edges": edges, "params": clusters_payload.get("params", {})}


def compute_global_prototype_clusters_hdbscan(
    *,
    min_cluster_size: int = 5,
    min_samples: int | None = None,
    include_logs_per_cluster: int = 20,
) -> Dict[str, Any]:
    """Cluster prototypes across all OS using HDBSCAN to build robust global clusters.

    Returns:
      {
        "params": {...},
        "clusters": [
          {
            "id": "gcluster_0",
            "size": 12,
            "centroid": [...],
            "medoid_document": "...",
            "source_breakdown": {...},
            "os_breakdown": {...},
            "sample_logs": [{ id, document, os, source, raw }]
          },
          ...
        ]
      }
    """
    if hdbscan is None:
        raise RuntimeError("HDBSCAN is not installed. Please install the 'hdbscan' package.")
    # Load prototypes from all OS
    provider = ChromaClientProvider()
    p_ids: List[str] = []
    p_docs: List[str] = []
    p_embs: List[List[float]] = []
    p_metas: List[Dict[str, Any]] = []

    for os_name in ("linux", "macos", "windows", "network"):
        try:
            coll = provider.get_or_create_collection(_proto_collection_name(os_name))
            data = coll.get(include=["embeddings", "documents", "metadatas"]) or {}
        except Exception as exc:
            LOG.info("hdbscan correlation: failed to read prototypes for os=%s err=%s", os_name, exc)
            data = {}
        ids0: List[str] = list(data.get("ids", []))
        docs0: List[str] = list(data.get("documents", []))
        embs0: List[List[float]] = list(data.get("embeddings", []))
        metas_raw = data.get("metadatas", []) or []
        metas0: List[Dict[str, Any]] = [m or {} for m in metas_raw]
        # Annotate os if missing
        for i in range(len(ids0)):
            meta = dict(metas0[i] or {})
            if "os" not in meta:
                meta["os"] = os_name
            p_ids.append(ids0[i])
            p_docs.append(docs0[i] if i < len(docs0) else "")
            p_embs.append(embs0[i] if i < len(embs0) else [])
            p_metas.append(meta)

    if not p_embs:
        return {
            "params": {
                "algorithm": "hdbscan",
                "basis": "prototypes",
                "min_cluster_size": min_cluster_size,
                "min_samples": min_samples if min_samples is not None else min_cluster_size,
                "include_logs_per_cluster": include_logs_per_cluster,
            },
            "clusters": [],
        }

    # Normalize to unit vectors for cosine-friendly medoids while using Euclidean in HDBSCAN
    X = np.asarray(p_embs, dtype=float)
    # Guard zero vectors
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X_norm = X / norms

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=int(max(2, min_cluster_size)),
        min_samples=int(max(1, min_samples if (min_samples is not None) else min_cluster_size)),
        metric="euclidean",
        cluster_selection_method="eom",
    )
    labels = clusterer.fit_predict(X_norm)

    # Group indices by label (ignore noise label -1)
    label_to_indices: Dict[int, List[int]] = {}
    for idx, lab in enumerate(labels):
        if int(lab) < 0:
            continue
        label_to_indices.setdefault(int(lab), []).append(idx)

    # Build clusters
    out_clusters: List[Dict[str, Any]] = []
    for lab, member_indices in label_to_indices.items():
        if not member_indices:
            continue
        # Compute centroid in normalized space and medoid
        centroid_vec = np.mean(X_norm[member_indices, :], axis=0)
        # Convert to list for downstream
        centroid = centroid_vec.tolist()
        # Compute medoid using cosine distance on normalized vectors
        medoid_global_idx = _compute_medoid_index(member_indices, [r.tolist() for r in X_norm], centroid)

        # Aggregate source/os counts by sampling logs assigned to each prototype id
        src_counts: Dict[str, int] = {}
        os_counts: Dict[str, int] = {}
        samples: List[Dict[str, Any]] = []
        # Round-robin across member prototypes to gather samples
        per_proto_cap = max(1, include_logs_per_cluster // max(1, len(member_indices)))
        for gi in member_indices:
            if len(samples) >= include_logs_per_cluster:
                break
            meta = p_metas[gi] if gi < len(p_metas) else {}
            osn = str((meta or {}).get("os") or "")
            proto_id = p_ids[gi] if gi < len(p_ids) else ""
            if not proto_id:
                continue
            try:
                lcoll = provider.get_or_create_collection(_logs_collection_name(osn))
                # Use get with where filter; query requires embeddings/documents which we are not providing here
                q = lcoll.get(
                    where={"cluster_id": proto_id},
                    include=["documents", "metadatas"],
                    limit=int(per_proto_cap),
                ) or {}
            except Exception as exc:
                LOG.info("hdbscan correlation: logs query failed os=%s proto=%s err=%s", osn, proto_id, exc)
                q = {}
            ids1 = list(q.get("ids", []))
            docs1 = list(q.get("documents", []))
            metas1 = list(q.get("metadatas", []))
            for j in range(len(ids1)):
                if len(samples) >= include_logs_per_cluster:
                    break
                mm = metas1[j] if j < len(metas1) else {}
                src = str((mm or {}).get("source") or "")
                o2 = str((mm or {}).get("os") or osn)
                src_counts[src] = src_counts.get(src, 0) + 1
                os_counts[o2] = os_counts.get(o2, 0) + 1
                samples.append({
                    "id": ids1[j],
                    "document": docs1[j] if j < len(docs1) else "",
                    "os": o2,
                    "source": src,
                    "raw": (mm or {}).get("raw", ""),
                })

        out_clusters.append({
            "id": f"gcluster_{lab}",
            "size": len(member_indices),
            "centroid": centroid,
            "medoid_document": p_docs[medoid_global_idx] if medoid_global_idx < len(p_docs) else "",
            "source_breakdown": src_counts,
            "os_breakdown": os_counts,
            "sample_logs": samples,
        })

    result = {
        "params": {
            "algorithm": "hdbscan",
            "basis": "prototypes",
            "min_cluster_size": int(max(2, min_cluster_size)),
            "min_samples": int(max(1, min_samples if (min_samples is not None) else min_cluster_size)),
            "include_logs_per_cluster": include_logs_per_cluster,
        },
        "clusters": out_clusters,
    }
    return result




