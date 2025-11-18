from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Query

from app.core.config import settings
from app.services.cross_correlation import (
    compute_global_clusters,
    build_graph_from_clusters,
    compute_global_prototype_clusters_hdbscan,
)

router = APIRouter()


@router.get("/correlation/global", response_model=Dict[str, Any])
async def get_global_correlation(
    limit_per_source: int = Query(200, ge=1, le=2000, description="Max logs per distinct source (for logs basis)"),
    threshold: float | None = Query(None, description="Override cluster distance threshold (for single-pass logs)"),
    min_size: int | None = Query(None, description="Override minimum cluster size (for single-pass logs)"),
    include_logs_per_cluster: int = Query(20, ge=0, le=200, description="Sample logs per cluster in response"),
    algorithm: str = Query("hdbscan", description='Clustering algorithm: "hdbscan" | "single_pass"'),
    basis: str = Query("prototypes", description='Clustering basis: "prototypes" | "logs"'),
    min_cluster_size: int = Query(5, ge=2, le=1000, description="HDBSCAN min_cluster_size when algorithm=hdbscan"),
    min_samples: int | None = Query(None, description="HDBSCAN min_samples when algorithm=hdbscan (default=min_cluster_size)"),
) -> Dict[str, Any]:
    """Compute cross-source clusters across all OS.
    
    Default: HDBSCAN over prototypes (basis=prototypes).
    Fallback: single-pass over logs when basis=logs.
    """
    # Preferred path: HDBSCAN over prototypes
    if basis == "prototypes" and algorithm == "hdbscan":
        proto_result = compute_global_prototype_clusters_hdbscan(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            include_logs_per_cluster=include_logs_per_cluster,
        )
        clusters = proto_result.get("clusters") or []
        if clusters:
            return proto_result

        # Demo-friendly fallback: if no prototype clusters found, fall back to
        # logs-based single-pass clustering so the UI can still display something.
        fallback_threshold = (
            threshold if threshold is not None else settings.CLUSTER_DISTANCE_THRESHOLD
        )
        # Be slightly more permissive than the default to encourage forming clusters
        default_min = getattr(settings, "CLUSTER_MIN_SIZE", 5)
        fallback_min_size = (
            min_size if min_size is not None else max(2, int(default_min) // 2)
        )
        logs_result = compute_global_clusters(
            limit_per_source=limit_per_source,
            threshold=fallback_threshold,
            min_size=fallback_min_size,
            include_logs_per_cluster=include_logs_per_cluster,
        )
        params = logs_result.setdefault("params", {})
        params.setdefault("basis", "logs")
        params.setdefault("algorithm", "single_pass")
        return logs_result

    # Explicit logs-based path (or non-HDBSCAN algorithm)
    return compute_global_clusters(
        limit_per_source=limit_per_source,
        threshold=threshold,
        min_size=min_size,
        include_logs_per_cluster=include_logs_per_cluster,
    )


@router.get("/correlation/graph", response_model=Dict[str, Any])
async def get_global_correlation_graph(
    limit_per_source: int = Query(200, ge=1, le=2000, description="Max logs per distinct source (for logs basis)"),
    threshold: float | None = Query(None, description="Override cluster distance threshold (for single-pass logs)"),
    min_size: int | None = Query(None, description="Override minimum cluster size (for single-pass logs)"),
    include_logs_per_cluster: int = Query(5, ge=0, le=50, description="Keep sample size small for graph view"),
    algorithm: str = Query("hdbscan", description='Clustering algorithm: "hdbscan" | "single_pass"'),
    basis: str = Query("prototypes", description='Clustering basis: "prototypes" | "logs"'),
    min_cluster_size: int = Query(5, ge=2, le=1000, description="HDBSCAN min_cluster_size when algorithm=hdbscan"),
    min_samples: int | None = Query(None, description="HDBSCAN min_samples when algorithm=hdbscan (default=min_cluster_size)"),
) -> Dict[str, Any]:
    """Return graph representation of cross-source clusters."""
    base: Dict[str, Any]

    if basis == "prototypes" and algorithm == "hdbscan":
        proto_result = compute_global_prototype_clusters_hdbscan(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            include_logs_per_cluster=include_logs_per_cluster,
        )
        clusters = proto_result.get("clusters") or []
        if clusters:
            base = proto_result
        else:
            # Same demo-friendly fallback as /correlation/global
            fallback_threshold = (
                threshold if threshold is not None else settings.CLUSTER_DISTANCE_THRESHOLD
            )
            default_min = getattr(settings, "CLUSTER_MIN_SIZE", 5)
            fallback_min_size = (
                min_size if min_size is not None else max(2, int(default_min) // 2)
            )
            base = compute_global_clusters(
                limit_per_source=limit_per_source,
                threshold=fallback_threshold,
                min_size=fallback_min_size,
                include_logs_per_cluster=include_logs_per_cluster,
            )
            params = base.setdefault("params", {})
            params.setdefault("basis", "logs")
            params.setdefault("algorithm", "single_pass")
    else:
        base = compute_global_clusters(
            limit_per_source=limit_per_source,
            threshold=threshold,
            min_size=min_size,
            include_logs_per_cluster=include_logs_per_cluster,
        )

    return build_graph_from_clusters(base)





