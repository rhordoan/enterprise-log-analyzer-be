from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Query
import redis.asyncio as aioredis

from app.core.config import settings
from app.services.cluster_metrics import ClusterMetricsTracker
from app.services.chroma_service import ChromaClientProvider, collection_name_for_os
from app.services.clustering_service import _single_pass_cluster
from app.services.cluster_metrics import (
    calculate_silhouette_score,
    calculate_cohesion,
    calculate_separation,
)

router = APIRouter()


@router.get("/clusters/{os_name}", response_model=Dict[str, Any])
async def get_cluster_health(
    os_name: str,
    hours: int = Query(24, ge=1, le=168, description="Hours of data to retrieve (max 7 days)")
) -> Dict[str, Any]:
    """Get overall cluster health metrics for an OS.
    
    Returns cluster count, size distribution, and quality trends.
    """
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    tracker = ClusterMetricsTracker(redis_client)
    
    try:
        # Get latest batch clustering metrics
        quality_metrics = await tracker.get_quality_metrics(os_name, hours=hours)
        online_metrics = await tracker.get_online_metrics(os_name, hours=hours)
        
        # Compute summary statistics
        latest_quality = quality_metrics[0] if quality_metrics else {}
        
        # Calculate trends
        new_cluster_rate = 0.0
        if online_metrics:
            total_new = sum(m.get("new_clusters", 0) for m in online_metrics)
            total_assignments = sum(m.get("total_assignments", 0) for m in online_metrics)
            new_cluster_rate = (total_new / total_assignments * 100) if total_assignments > 0 else 0.0
        
        return {
            "os": os_name,
            "latest_quality": {
                "silhouette_score": latest_quality.get("silhouette_score", 0.0),
                "cohesion": latest_quality.get("cohesion", 0.0),
                "separation": latest_quality.get("separation", 0.0),
                "num_clusters": latest_quality.get("num_clusters", 0),
                "timestamp": latest_quality.get("timestamp", ""),
            },
            "online_stats": {
                "new_cluster_rate_pct": round(new_cluster_rate, 2),
                "total_assignments": sum(m.get("total_assignments", 0) for m in online_metrics),
                "total_new_clusters": sum(m.get("new_clusters", 0) for m in online_metrics),
            },
            "quality_history": quality_metrics[:10],  # Last 10 batch clustering runs
            "online_history": online_metrics[:24],  # Last 24 hours
        }
    finally:
        await redis_client.close()


@router.get("/quality/{os_name}", response_model=Dict[str, Any])
async def get_quality_metrics(
    os_name: str,
    hours: int = Query(24, ge=1, le=168, description="Hours of data to retrieve")
) -> Dict[str, Any]:
    """Get detailed quality metrics over time.
    
    Returns silhouette scores, cohesion, and separation trends.
    """
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    tracker = ClusterMetricsTracker(redis_client)
    
    try:
        metrics = await tracker.get_quality_metrics(os_name, hours=hours)
        
        # Calculate quality assessment
        latest = metrics[0] if metrics else {}
        silhouette = latest.get("silhouette_score", 0.0)
        
        if silhouette > 0.5:
            quality_assessment = "excellent"
        elif silhouette > 0.3:
            quality_assessment = "good"
        elif silhouette > 0.1:
            quality_assessment = "weak"
        else:
            quality_assessment = "poor"
        
        # Recommendations
        recommendations = []
        if silhouette < 0.3:
            recommendations.append(f"Consider reducing CLUSTER_DISTANCE_THRESHOLD (current: {latest.get('threshold', 'N/A')}) to create tighter clusters")
        
        cohesion = latest.get("cohesion", 0.0)
        if cohesion > 0.3:
            recommendations.append("High cohesion indicates loose clusters; consider decreasing distance threshold")
        
        separation = latest.get("separation", 0.0)
        if separation < 0.3:
            recommendations.append("Low separation indicates overlapping clusters; consider increasing CLUSTER_MIN_SIZE")
        
        return {
            "os": os_name,
            "quality_assessment": quality_assessment,
            "latest_metrics": latest,
            "recommendations": recommendations,
            "history": metrics,
        }
    finally:
        await redis_client.close()


@router.get("/quality/compute/{os_name}", response_model=Dict[str, Any])
async def compute_current_quality(
    os_name: str,
    include_logs_samples: int = Query(0, ge=0, le=2000, description="Optional number of log embeddings to include"),
    threshold: float | None = Query(None, description="Override cluster distance threshold"),
    min_size: int | None = Query(None, description="Override minimum cluster size"),
) -> Dict[str, Any]:
    """Compute current clustering quality metrics on-demand.

    Uses current embeddings from templates_<os> and optionally a sample from logs_<os>.
    Returns silhouette score, cohesion, separation, cluster counts and basic stats.
    """
    provider = ChromaClientProvider()

    # Load template embeddings
    tcoll = provider.get_or_create_collection(collection_name_for_os(os_name))
    t_data = tcoll.get(include=["embeddings", "documents", "ids"]) or {}
    t_embs = list(t_data.get("embeddings", []))

    # Optionally include a sample of logs embeddings
    if include_logs_samples and include_logs_samples > 0:
        lcoll_name = f"{settings.CHROMA_LOG_COLLECTION_PREFIX}{os_name}"
        lcoll = provider.get_or_create_collection(lcoll_name)
        l_data = lcoll.get(include=["embeddings", "ids"], limit=int(include_logs_samples)) or {}
        t_embs.extend(l_data.get("embeddings", []))

    if not t_embs:
        return {
            "os": os_name,
            "error": "no_embeddings",
            "message": "No embeddings found in templates/logs collections",
        }

    thr = threshold if threshold is not None else settings.CLUSTER_DISTANCE_THRESHOLD
    ms = min_size if min_size is not None else settings.CLUSTER_MIN_SIZE

    # Cluster and compute metrics
    clusters, _centroids = _single_pass_cluster(t_embs, threshold=thr, min_size=ms)

    silhouette = calculate_silhouette_score(clusters, t_embs)
    cohesion = calculate_cohesion(clusters, t_embs)
    separation = calculate_separation(clusters, t_embs)

    sizes = [len(c) for c in clusters]

    return {
        "os": os_name,
        "params": {"threshold": thr, "min_size": ms, "include_logs_samples": include_logs_samples},
        "num_clusters": len(clusters),
        "num_points": len(t_embs),
        "silhouette_score": silhouette,
        "cohesion": cohesion,
        "separation": separation,
        "cluster_sizes": sizes,
    }


@router.get("/llm-usage", response_model=Dict[str, Any])
async def get_llm_usage(
    hours: int = Query(24, ge=1, le=168, description="Hours of data to retrieve")
) -> Dict[str, Any]:
    """Get LLM usage metrics including costs, latency, and confidence.
    
    Returns aggregated metrics across all LLM calls.
    """
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    tracker = ClusterMetricsTracker(redis_client)
    
    try:
        metrics = await tracker.get_llm_metrics(hours=hours)
        
        # Aggregate totals
        total_calls = sum(m.get("total_calls", 0) for m in metrics)
        total_cost = sum(m.get("total_cost_usd", 0) for m in metrics)
        total_tokens = sum(m.get("total_tokens", 0) for m in metrics)
        successful_calls = sum(m.get("successful_calls", 0) for m in metrics)
        
        success_rate = (successful_calls / total_calls * 100) if total_calls > 0 else 0.0
        avg_cost_per_call = (total_cost / total_calls) if total_calls > 0 else 0.0
        
        # Calculate average latency
        total_latency = sum(m.get("total_latency_ms", 0) for m in metrics)
        avg_latency = (total_latency / total_calls) if total_calls > 0 else 0.0
        
        # Cost recommendations
        recommendations = []
        if avg_cost_per_call > 0.01:
            recommendations.append("High cost per call; consider implementing semantic caching or reducing prompt length")
        
        if success_rate < 90:
            recommendations.append(f"Low success rate ({success_rate:.1f}%); check LLM service health")
        
        if avg_latency > 5000:
            recommendations.append(f"High latency ({avg_latency:.0f}ms); consider optimizing prompts or switching models")
        
        return {
            "summary": {
                "total_calls": total_calls,
                "successful_calls": successful_calls,
                "failed_calls": total_calls - successful_calls,
                "success_rate_pct": round(success_rate, 2),
                "total_cost_usd": round(total_cost, 4),
                "total_tokens": total_tokens,
                "avg_cost_per_call_usd": round(avg_cost_per_call, 6),
                "avg_latency_ms": round(avg_latency, 2),
            },
            "recommendations": recommendations,
            "hourly_breakdown": metrics,
        }
    finally:
        await redis_client.close()


@router.get("/drift/{os_name}", response_model=Dict[str, Any])
async def get_drift_metrics(
    os_name: str,
    hours: int = Query(24, ge=1, le=168, description="Hours of data to retrieve")
) -> Dict[str, Any]:
    """Get drift detection metrics for an OS.
    
    Returns new cluster creation rates and trends that indicate changing log patterns.
    """
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    tracker = ClusterMetricsTracker(redis_client)
    
    try:
        metrics = await tracker.get_online_metrics(os_name, hours=hours)
        
        # Calculate drift indicators
        drift_signals = []
        
        # Check for sudden spikes in new cluster creation
        if len(metrics) >= 2:
            recent_rate = metrics[0].get("new_clusters", 0) / max(metrics[0].get("total_assignments", 1), 1)
            avg_rate = sum(m.get("new_clusters", 0) for m in metrics) / sum(m.get("total_assignments", 1) for m in metrics)
            
            if recent_rate > avg_rate * 2:
                drift_signals.append({
                    "type": "spike",
                    "severity": "high",
                    "message": f"New cluster creation rate is {recent_rate/avg_rate:.1f}x higher than average",
                })
        
        # Check for sustained high new cluster rate
        recent_hours = metrics[:6]  # Last 6 hours
        if recent_hours:
            recent_new_rate = sum(m.get("new_clusters", 0) for m in recent_hours) / sum(m.get("total_assignments", 1) for m in recent_hours)
            if recent_new_rate > 0.1:  # More than 10% of logs creating new clusters
                drift_signals.append({
                    "type": "sustained_drift",
                    "severity": "medium",
                    "message": f"{recent_new_rate*100:.1f}% of logs are creating new clusters (threshold: 10%)",
                })
        
        # Recommendations based on drift
        recommendations = []
        if drift_signals:
            recommendations.append("Log patterns are changing significantly; consider running batch re-clustering")
            recommendations.append("Investigate recent deployments or system changes")
        
        if not drift_signals:
            drift_status = "stable"
        elif any(s.get("severity") == "high" for s in drift_signals):
            drift_status = "high_drift"
        else:
            drift_status = "moderate_drift"
        
        return {
            "os": os_name,
            "drift_status": drift_status,
            "drift_signals": drift_signals,
            "recommendations": recommendations,
            "metrics": metrics,
        }
    finally:
        await redis_client.close()




