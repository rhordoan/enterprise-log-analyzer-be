from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import math

import redis.asyncio as aioredis

from app.core.config import settings

LOG = logging.getLogger(__name__)


def _cosine_distance(a: List[float], b: List[float]) -> float:
    """Calculate cosine distance between two normalized vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    dot = max(min(dot, 1.0), -1.0)
    return 1.0 - dot


def calculate_silhouette_score(
    clusters: List[List[int]],
    embeddings: List[List[float]],
) -> float:
    """Calculate average silhouette score for all clusters.
    
    Silhouette score ranges from -1 to 1:
    - 1: Perfect clustering (samples far from neighboring clusters)
    - 0: Samples on or very close to decision boundary
    - -1: Samples assigned to wrong clusters
    """
    if len(clusters) < 2:
        return 0.0
    
    total_score = 0.0
    total_samples = 0
    
    for cluster_idx, cluster_members in enumerate(clusters):
        if len(cluster_members) < 2:
            continue
        
        for member_idx in cluster_members:
            member_vec = embeddings[member_idx]
            
            # Calculate a(i): mean distance to other points in same cluster
            intra_distances = []
            for other_idx in cluster_members:
                if other_idx != member_idx:
                    intra_distances.append(_cosine_distance(member_vec, embeddings[other_idx]))
            
            a_i = sum(intra_distances) / len(intra_distances) if intra_distances else 0.0
            
            # Calculate b(i): mean distance to points in nearest other cluster
            min_inter_distance = float('inf')
            for other_cluster_idx, other_cluster in enumerate(clusters):
                if other_cluster_idx == cluster_idx or not other_cluster:
                    continue
                
                inter_distances = [
                    _cosine_distance(member_vec, embeddings[other_idx])
                    for other_idx in other_cluster
                ]
                mean_inter = sum(inter_distances) / len(inter_distances) if inter_distances else 0.0
                min_inter_distance = min(min_inter_distance, mean_inter)
            
            b_i = min_inter_distance if min_inter_distance != float('inf') else 0.0
            
            # Silhouette score for this sample
            if max(a_i, b_i) > 0:
                s_i = (b_i - a_i) / max(a_i, b_i)
            else:
                s_i = 0.0
            
            total_score += s_i
            total_samples += 1
    
    return total_score / total_samples if total_samples > 0 else 0.0


def calculate_cohesion(clusters: List[List[int]], embeddings: List[List[float]]) -> float:
    """Calculate average intra-cluster distance (cohesion).
    
    Lower values indicate tighter, more cohesive clusters.
    """
    if not clusters:
        return 0.0
    
    total_distance = 0.0
    total_pairs = 0
    
    for cluster_members in clusters:
        if len(cluster_members) < 2:
            continue
        
        for i, idx_i in enumerate(cluster_members):
            for idx_j in cluster_members[i + 1:]:
                total_distance += _cosine_distance(embeddings[idx_i], embeddings[idx_j])
                total_pairs += 1
    
    return total_distance / total_pairs if total_pairs > 0 else 0.0


def calculate_separation(clusters: List[List[int]], embeddings: List[List[float]]) -> float:
    """Calculate average inter-cluster distance (separation).
    
    Higher values indicate better separated clusters.
    """
    if len(clusters) < 2:
        return 1.0
    
    # Calculate centroids
    centroids = []
    for cluster_members in clusters:
        if not cluster_members:
            continue
        cluster_vecs = [embeddings[idx] for idx in cluster_members]
        centroid = [sum(v[i] for v in cluster_vecs) / len(cluster_vecs) for i in range(len(cluster_vecs[0]))]
        centroids.append(centroid)
    
    if len(centroids) < 2:
        return 1.0
    
    # Calculate pairwise centroid distances
    total_distance = 0.0
    total_pairs = 0
    
    for i in range(len(centroids)):
        for j in range(i + 1, len(centroids)):
            total_distance += _cosine_distance(centroids[i], centroids[j])
            total_pairs += 1
    
    return total_distance / total_pairs if total_pairs > 0 else 0.0


def calculate_distance_stats(distances: List[float]) -> Dict[str, float]:
    """Calculate statistics for a list of distances."""
    if not distances:
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0, "count": 0}
    
    mean = sum(distances) / len(distances)
    variance = sum((d - mean) ** 2 for d in distances) / len(distances)
    std = math.sqrt(variance)
    
    return {
        "mean": mean,
        "std": std,
        "min": min(distances),
        "max": max(distances),
        "count": len(distances),
    }


class ClusterMetricsTracker:
    """Tracks and stores cluster metrics in Redis."""
    
    def __init__(self, redis_client: aioredis.Redis):
        self.redis = redis_client
    
    async def record_batch_clustering_metrics(
        self,
        os_name: str,
        clusters: List[List[int]],
        embeddings: List[List[float]],
        threshold: float,
        min_size: int,
    ) -> Dict[str, Any]:
        """Record metrics from batch clustering operation."""
        timestamp = datetime.utcnow().isoformat() + 'Z'
        
        # Calculate quality metrics
        silhouette = calculate_silhouette_score(clusters, embeddings)
        cohesion = calculate_cohesion(clusters, embeddings)
        separation = calculate_separation(clusters, embeddings)
        
        # Cluster size distribution
        sizes = [len(c) for c in clusters]
        size_stats = calculate_distance_stats([float(s) for s in sizes])
        
        metrics = {
            "timestamp": timestamp,
            "os": os_name,
            "type": "batch_clustering",
            "num_clusters": len(clusters),
            "num_logs": len(embeddings),
            "silhouette_score": silhouette,
            "cohesion": cohesion,
            "separation": separation,
            "threshold": threshold,
            "min_size": min_size,
            "cluster_size_mean": size_stats["mean"],
            "cluster_size_std": size_stats["std"],
            "cluster_size_min": size_stats["min"],
            "cluster_size_max": size_stats["max"],
        }
        
        # Store in Redis with TTL (keep for 7 days)
        key = f"cluster_metrics:batch:{os_name}:{int(time.time())}"
        await self.redis.setex(key, 7 * 24 * 3600, json.dumps(metrics))
        
        # Update latest metrics
        latest_key = f"cluster_metrics:latest:batch:{os_name}"
        await self.redis.setex(latest_key, 7 * 24 * 3600, json.dumps(metrics))
        
        LOG.info(
            "batch clustering metrics os=%s clusters=%d silhouette=%.3f cohesion=%.3f separation=%.3f",
            os_name, len(clusters), silhouette, cohesion, separation
        )
        
        return metrics
    
    async def record_online_cluster_assignment(
        self,
        os_name: str,
        cluster_id: str,
        distance: float,
        is_new_cluster: bool,
    ) -> None:
        """Record a single online cluster assignment."""
        timestamp = datetime.utcnow().isoformat() + 'Z'
        
        # Increment counters
        hour_key = f"cluster_metrics:online:{os_name}:{datetime.utcnow().strftime('%Y-%m-%d-%H')}"
        await self.redis.hincrby(hour_key, "total_assignments", 1)
        await self.redis.expire(hour_key, 7 * 24 * 3600)
        
        if is_new_cluster:
            await self.redis.hincrby(hour_key, "new_clusters", 1)
        
        # Track distance distribution (store in sorted set for percentile queries)
        distance_key = f"cluster_metrics:distances:{os_name}:{datetime.utcnow().strftime('%Y-%m-%d-%H')}"
        await self.redis.zadd(distance_key, {f"{timestamp}:{cluster_id}": distance})
        await self.redis.expire(distance_key, 7 * 24 * 3600)
    
    async def record_llm_call(
        self,
        os_name: str,
        cluster_id: str,
        operation: str,
        confidence: Optional[float],
        tokens_used: int,
        latency_ms: float,
        success: bool,
    ) -> None:
        """Record LLM call metrics."""
        timestamp = datetime.utcnow().isoformat() + 'Z'
        
        # Aggregate metrics by hour
        hour_key = f"cluster_metrics:llm:{datetime.utcnow().strftime('%Y-%m-%d-%H')}"
        await self.redis.hincrby(hour_key, "total_calls", 1)
        await self.redis.hincrbyfloat(hour_key, "total_tokens", float(tokens_used))
        await self.redis.hincrbyfloat(hour_key, "total_latency_ms", latency_ms)
        await self.redis.expire(hour_key, 7 * 24 * 3600)
        
        if success:
            await self.redis.hincrby(hour_key, "successful_calls", 1)
        else:
            await self.redis.hincrby(hour_key, "failed_calls", 1)
        
        # Track confidence distribution
        if confidence is not None:
            confidence_key = f"cluster_metrics:llm:confidence:{datetime.utcnow().strftime('%Y-%m-%d-%H')}"
            await self.redis.zadd(confidence_key, {f"{timestamp}:{cluster_id}": confidence})
            await self.redis.expire(confidence_key, 7 * 24 * 3600)
        
        # Calculate cost (using OpenAI pricing as default)
        cost = (tokens_used / 1000.0) * settings.LLM_COST_PER_1K_TOKENS
        await self.redis.hincrbyfloat(hour_key, "total_cost_usd", cost)
    
    async def get_quality_metrics(self, os_name: str, hours: int = 24) -> List[Dict[str, Any]]:
        """Retrieve quality metrics for the last N hours."""
        pattern = f"cluster_metrics:batch:{os_name}:*"
        keys = []
        cursor = 0
        
        # Scan for matching keys
        while True:
            cursor, batch = await self.redis.scan(cursor, match=pattern, count=100)
            keys.extend(batch)
            if cursor == 0:
                break
        
        # Retrieve and parse metrics
        metrics = []
        for key in keys:
            data = await self.redis.get(key)
            if data:
                try:
                    metrics.append(json.loads(data))
                except json.JSONDecodeError:
                    pass
        
        # Sort by timestamp (most recent first)
        metrics.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        return metrics
    
    async def get_online_metrics(self, os_name: str, hours: int = 24) -> List[Dict[str, Any]]:
        """Retrieve online clustering metrics for the last N hours."""
        metrics = []
        now = datetime.utcnow()
        
        for hour_offset in range(hours):
            hour_dt = now - timedelta(hours=hour_offset)
            hour_str = hour_dt.strftime('%Y-%m-%d-%H')
            
            hour_key = f"cluster_metrics:online:{os_name}:{hour_str}"
            data = await self.redis.hgetall(hour_key)
            
            if data:
                metrics.append({
                    "hour": hour_str,
                    "total_assignments": int(data.get("total_assignments", 0)),
                    "new_clusters": int(data.get("new_clusters", 0)),
                })
        
        return metrics
    
    async def get_llm_metrics(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Retrieve LLM usage metrics for the last N hours."""
        metrics = []
        now = datetime.utcnow()
        
        for hour_offset in range(hours):
            hour_dt = now - timedelta(hours=hour_offset)
            hour_str = hour_dt.strftime('%Y-%m-%d-%H')
            
            hour_key = f"cluster_metrics:llm:{hour_str}"
            data = await self.redis.hgetall(hour_key)
            
            if data:
                total_calls = int(data.get("total_calls", 0))
                metrics.append({
                    "hour": hour_str,
                    "total_calls": total_calls,
                    "successful_calls": int(data.get("successful_calls", 0)),
                    "failed_calls": int(data.get("failed_calls", 0)),
                    "total_tokens": int(float(data.get("total_tokens", 0))),
                    "total_latency_ms": float(data.get("total_latency_ms", 0)),
                    "total_cost_usd": float(data.get("total_cost_usd", 0)),
                    "avg_latency_ms": float(data.get("total_latency_ms", 0)) / total_calls if total_calls > 0 else 0,
                })
        
        return metrics

