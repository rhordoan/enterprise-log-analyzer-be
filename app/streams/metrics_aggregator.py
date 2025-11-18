from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List

import redis.asyncio as aioredis
from fastapi import FastAPI

from app.core.config import get_settings
from app.services.chroma_service import ChromaClientProvider
from app.services.cluster_metrics import ClusterMetricsTracker

settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
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


async def aggregate_cluster_stats(os_name: str) -> Dict[str, Any]:
    """Aggregate statistics about current clusters for an OS."""
    try:
        provider = ChromaClientProvider()
        collection = provider.get_or_create_collection(_proto_collection_name(os_name))
        
        # Get all prototypes
        data = collection.get(include=["metadatas", "embeddings"]) or {}
        metas = data.get("metadatas", [])
        embeddings = data.get("embeddings", [])
        
        if not metas:
            return {
                "os": os_name,
                "total_clusters": 0,
                "avg_size": 0,
                "labeled_clusters": 0,
            }
        
        # Aggregate stats
        total_clusters = len(metas)
        sizes = [m.get("size", 1) for m in metas]
        labeled = sum(1 for m in metas if m.get("label") and m.get("label") != "unknown")
        
        avg_size = sum(sizes) / len(sizes) if sizes else 0
        max_size = max(sizes) if sizes else 0
        min_size = min(sizes) if sizes else 0
        
        # Count by label
        label_counts: Dict[str, int] = {}
        for m in metas:
            label = m.get("label", "unknown")
            label_counts[label] = label_counts.get(label, 0) + 1
        
        return {
            "os": os_name,
            "total_clusters": total_clusters,
            "labeled_clusters": labeled,
            "unlabeled_clusters": total_clusters - labeled,
            "avg_size": round(avg_size, 2),
            "max_size": max_size,
            "min_size": min_size,
            "label_distribution": label_counts,
        }
    except Exception as exc:
        LOG.error("Failed to aggregate cluster stats for os=%s err=%s", os_name, exc)
        return {"os": os_name, "error": str(exc)}


async def check_quality_alerts(os_name: str, tracker: ClusterMetricsTracker) -> List[Dict[str, Any]]:
    """Check if quality metrics are below thresholds and generate alerts."""
    alerts = []
    
    try:
        # Get latest quality metrics
        quality_metrics = await tracker.get_quality_metrics(os_name, hours=1)
        
        if not quality_metrics:
            return alerts
        
        latest = quality_metrics[0]
        silhouette = latest.get("silhouette_score", 0.0)
        
        # Check against threshold
        if silhouette < settings.CLUSTER_QUALITY_THRESHOLD:
            alerts.append({
                "type": "low_quality",
                "severity": "warning",
                "os": os_name,
                "message": f"Silhouette score ({silhouette:.3f}) below threshold ({settings.CLUSTER_QUALITY_THRESHOLD})",
                "timestamp": datetime.utcnow().isoformat() + 'Z',
                "metric": "silhouette_score",
                "value": silhouette,
                "threshold": settings.CLUSTER_QUALITY_THRESHOLD,
            })
    except Exception as exc:
        LOG.error("Failed to check quality alerts for os=%s err=%s", os_name, exc)
    
    return alerts


async def check_drift_alerts(os_name: str, tracker: ClusterMetricsTracker) -> List[Dict[str, Any]]:
    """Check for drift indicators and generate alerts."""
    alerts = []
    
    try:
        # Get online metrics for drift detection window
        window_hours = settings.DRIFT_DETECTION_WINDOW_SEC // 3600
        metrics = await tracker.get_online_metrics(os_name, hours=window_hours)
        
        if len(metrics) < 2:
            return alerts
        
        # Calculate new cluster rate
        total_new = sum(m.get("new_clusters", 0) for m in metrics)
        total_assignments = sum(m.get("total_assignments", 0) for m in metrics)
        
        if total_assignments == 0:
            return alerts
        
        new_rate = total_new / total_assignments
        
        # Alert if more than 15% of assignments are creating new clusters
        if new_rate > 0.15:
            alerts.append({
                "type": "high_drift",
                "severity": "warning",
                "os": os_name,
                "message": f"High new cluster rate: {new_rate*100:.1f}% of logs creating new clusters",
                "timestamp": datetime.utcnow().isoformat() + 'Z',
                "metric": "new_cluster_rate",
                "value": new_rate,
                "threshold": 0.15,
            })
    except Exception as exc:
        LOG.error("Failed to check drift alerts for os=%s err=%s", os_name, exc)
    
    return alerts


async def run_metrics_aggregation():
    """Periodically aggregate metrics and check for alerts."""
    tracker = ClusterMetricsTracker(redis)
    
    while True:
        try:
            # Process each OS
            for os_name in ["linux", "macos", "windows"]:
                try:
                    # Aggregate current cluster stats
                    stats = await aggregate_cluster_stats(os_name)
                    
                    # Store aggregated stats
                    stats_key = f"cluster_metrics:aggregated:{os_name}:latest"
                    import json
                    await redis.setex(stats_key, 7 * 24 * 3600, json.dumps(stats))
                    
                    # Check for quality alerts
                    quality_alerts = await check_quality_alerts(os_name, tracker)
                    
                    # Check for drift alerts
                    drift_alerts = await check_drift_alerts(os_name, tracker)
                    
                    # Publish alerts to alerts stream
                    all_alerts = quality_alerts + drift_alerts
                    for alert in all_alerts:
                        try:
                            await redis.xadd(settings.ALERTS_STREAM, alert)
                            LOG.warning(
                                "cluster metric alert type=%s os=%s message=%s",
                                alert.get("type"),
                                alert.get("os"),
                                alert.get("message"),
                            )
                        except Exception:
                            pass
                    
                    LOG.info(
                        "metrics aggregation complete os=%s clusters=%d alerts=%d",
                        os_name,
                        stats.get("total_clusters", 0),
                        len(all_alerts),
                    )
                except Exception as exc:
                    LOG.error("Metrics aggregation failed for os=%s err=%s", os_name, exc)
            
        except Exception as exc:
            LOG.error("Metrics aggregation loop error err=%s", exc)
        
        # Wait for next interval
        await asyncio.sleep(settings.METRICS_AGGREGATION_INTERVAL_SEC)


def attach_metrics_aggregator(app: FastAPI):
    """Attach metrics aggregator as a background task."""
    
    async def _run_forever():
        backoff = 1.0
        while True:
            try:
                LOG.info("starting metrics aggregator interval=%ds", settings.METRICS_AGGREGATION_INTERVAL_SEC)
                await run_metrics_aggregation()
            except Exception as exc:
                LOG.error("metrics aggregator crashed err=%s; restarting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    
    @app.on_event("startup")
    async def startup_event():
        if not settings.ENABLE_CLUSTER_METRICS:
            LOG.info("metrics aggregator disabled via ENABLE_CLUSTER_METRICS=False")
            return
        
        LOG.info("starting metrics aggregator in dedicated thread")
        loop = asyncio.new_event_loop()
        
        def _runner():
            asyncio.set_event_loop(loop)
            loop.create_task(_run_forever())
            loop.run_forever()
        
        thread = threading.Thread(target=_runner, name="metrics-aggregator-thread", daemon=True)
        thread.start()
        app.state.metrics_aggregator_loop = loop
        app.state.metrics_aggregator_thread = thread
    
    @app.on_event("shutdown")
    async def shutdown_event():
        LOG.info("stopping metrics aggregator thread")
        loop = getattr(app.state, "metrics_aggregator_loop", None)
        thread = getattr(app.state, "metrics_aggregator_thread", None)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)




