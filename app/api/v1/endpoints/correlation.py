from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Query

from app.services.cross_correlation import compute_global_clusters, build_graph_from_clusters, compute_global_prototype_clusters_hdbscan

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
    if basis == "prototypes" and algorithm == "hdbscan":
        return compute_global_prototype_clusters_hdbscan(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            include_logs_per_cluster=include_logs_per_cluster,
        )
    # Fallback to existing logs-based single-pass clustering
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
    if basis == "prototypes" and algorithm == "hdbscan":
        base = compute_global_prototype_clusters_hdbscan(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            include_logs_per_cluster=include_logs_per_cluster,
        )
    else:
        base = compute_global_clusters(
            limit_per_source=limit_per_source,
            threshold=threshold,
            min_size=min_size,
            include_logs_per_cluster=include_logs_per_cluster,
        )
    return build_graph_from_clusters(base)





