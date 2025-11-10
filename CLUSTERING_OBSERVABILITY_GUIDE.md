# Clustering Observability Guide

## Overview

The clustering observability system provides comprehensive metrics and monitoring for the log clustering pipeline. It tracks cluster quality, LLM usage costs, and drift detection to enable data-driven improvements.

## Architecture

### Components

1. **Metrics Collection Service** (`app/services/cluster_metrics.py`)
   - Calculates quality metrics (silhouette score, cohesion, separation)
   - Stores metrics in Redis with 7-day retention
   - Provides query APIs for historical analysis

2. **Instrumentation Points**
   - `clustering_service.py`: Batch clustering quality metrics
   - `online_clustering.py`: Real-time cluster assignment tracking
   - `llm_service.py`: Token usage and latency tracking
   - `cluster_enricher.py`: LLM cost and confidence monitoring

3. **Metrics Aggregator** (`app/streams/metrics_aggregator.py`)
   - Runs every 5 minutes (configurable via `METRICS_AGGREGATION_INTERVAL_SEC`)
   - Aggregates cluster statistics across all OS types
   - Generates alerts for quality degradation and drift
   - Publishes alerts to Redis `alerts` stream

4. **API Endpoints** (`app/api/v1/endpoints/cluster_metrics.py`)
   - `/api/v1/metrics/clusters/{os_name}` - Overall cluster health
   - `/api/v1/metrics/quality/{os_name}` - Quality metrics over time
   - `/api/v1/metrics/llm-usage` - LLM costs and performance
   - `/api/v1/metrics/drift/{os_name}` - Drift detection signals

## API Usage

### 1. Get Cluster Health

```bash
curl http://localhost:8000/api/v1/metrics/clusters/linux?hours=24
```

**Response:**
```json
{
  "os": "linux",
  "latest_quality": {
    "silhouette_score": 0.45,
    "cohesion": 0.18,
    "separation": 0.62,
    "num_clusters": 42,
    "timestamp": "2024-01-15T10:30:00Z"
  },
  "online_stats": {
    "new_cluster_rate_pct": 5.2,
    "total_assignments": 1250,
    "total_new_clusters": 65
  },
  "quality_history": [...],
  "online_history": [...]
}
```

### 2. Get Quality Metrics

```bash
curl http://localhost:8000/api/v1/metrics/quality/macos?hours=168
```

**Response:**
```json
{
  "os": "macos",
  "quality_assessment": "good",
  "latest_metrics": {
    "silhouette_score": 0.38,
    "cohesion": 0.22,
    "separation": 0.58,
    "threshold": 0.2,
    "min_size": 5
  },
  "recommendations": [
    "Consider reducing CLUSTER_DISTANCE_THRESHOLD (current: 0.2) to create tighter clusters"
  ],
  "history": [...]
}
```

### 3. Get LLM Usage

```bash
curl http://localhost:8000/api/v1/metrics/llm-usage?hours=24
```

**Response:**
```json
{
  "summary": {
    "total_calls": 150,
    "successful_calls": 148,
    "failed_calls": 2,
    "success_rate_pct": 98.67,
    "total_cost_usd": 0.0234,
    "total_tokens": 234500,
    "avg_cost_per_call_usd": 0.000156,
    "avg_latency_ms": 1250.5
  },
  "recommendations": [],
  "hourly_breakdown": [...]
}
```

### 4. Get Drift Metrics

```bash
curl http://localhost:8000/api/v1/metrics/drift/windows?hours=48
```

**Response:**
```json
{
  "os": "windows",
  "drift_status": "moderate_drift",
  "drift_signals": [
    {
      "type": "sustained_drift",
      "severity": "medium",
      "message": "12.5% of logs are creating new clusters (threshold: 10%)"
    }
  ],
  "recommendations": [
    "Log patterns are changing significantly; consider running batch re-clustering",
    "Investigate recent deployments or system changes"
  ],
  "metrics": [...]
}
```

## Understanding Metrics

### Silhouette Score (-1 to 1)

The silhouette score measures how well-separated clusters are.

| Score Range | Quality | Action |
|------------|---------|--------|
| > 0.5 | Excellent | Clustering is working well |
| 0.3 - 0.5 | Good | Acceptable performance |
| 0.1 - 0.3 | Weak | Reduce `CLUSTER_DISTANCE_THRESHOLD` |
| < 0.1 | Poor | Major tuning needed |

**Formula:** For each log, compare average distance to logs in same cluster vs. nearest other cluster.

### Cohesion (Intra-cluster Distance)

Measures average distance between logs within the same cluster.

- **Lower is better** (tighter clusters)
- Typical range: 0.1 - 0.4 for cosine distance
- High cohesion (>0.3) → clusters are too loose

**Action:** If cohesion > 0.3, decrease `CLUSTER_DISTANCE_THRESHOLD`

### Separation (Inter-cluster Distance)

Measures average distance between cluster centroids.

- **Higher is better** (distinct clusters)
- Typical range: 0.4 - 0.8 for cosine distance
- Low separation (<0.3) → overlapping clusters

**Action:** If separation < 0.3, increase `CLUSTER_MIN_SIZE` to merge small clusters

### New Cluster Rate

Percentage of log assignments that create new clusters.

| Rate | Status | Interpretation |
|------|--------|----------------|
| < 5% | Stable | Patterns are well-known |
| 5-10% | Normal | Expected variance |
| 10-15% | Elevated | New patterns emerging |
| > 15% | High Drift | Major changes in logs |

**Action:** If rate > 15%, investigate recent deployments or run batch re-clustering

### LLM Metrics

#### Cost per Call
- Track `avg_cost_per_call_usd` to monitor spending
- Typical cost: $0.0001 - $0.001 per call (gpt-4o-mini)
- High cost → implement caching or reduce prompt length

#### Latency
- Track `avg_latency_ms` for performance
- Typical latency: 500-2000ms
- High latency (>5000ms) → optimize prompts or switch models

#### Confidence Distribution
- Monitor confidence scores from LLM classifications
- Target: 70%+ of calls with confidence > 0.7
- Low confidence → add more examples or improve prompts

## Alerts

The metrics aggregator generates alerts when thresholds are exceeded:

### Quality Alerts
```json
{
  "type": "low_quality",
  "severity": "warning",
  "os": "linux",
  "message": "Silhouette score (0.22) below threshold (0.3)",
  "metric": "silhouette_score",
  "value": 0.22,
  "threshold": 0.3
}
```

### Drift Alerts
```json
{
  "type": "high_drift",
  "severity": "warning",
  "os": "macos",
  "message": "High new cluster rate: 18.5% of logs creating new clusters",
  "metric": "new_cluster_rate",
  "value": 0.185,
  "threshold": 0.15
}
```

Alerts are published to the Redis stream: `alerts`

## Configuration

Add to `.env` file:

```bash
# Observability settings
ENABLE_CLUSTER_METRICS=true
METRICS_AGGREGATION_INTERVAL_SEC=300  # 5 minutes
CLUSTER_QUALITY_THRESHOLD=0.3  # Min silhouette score
DRIFT_DETECTION_WINDOW_SEC=3600  # 1 hour
LLM_COST_PER_1K_TOKENS=0.0001  # OpenAI gpt-4o-mini pricing
```

## Operational Workflow

### Daily

1. **Check Quality Dashboard**
   ```bash
   curl http://localhost:8000/api/v1/metrics/clusters/linux | jq '.latest_quality'
   ```
   - Verify silhouette score > 0.3
   - Check cluster count is stable

2. **Review LLM Costs**
   ```bash
   curl http://localhost:8000/api/v1/metrics/llm-usage | jq '.summary.total_cost_usd'
   ```
   - Track daily spending trends
   - Identify cost spikes

### Weekly

1. **Analyze Quality Trends**
   ```bash
   curl http://localhost:8000/api/v1/metrics/quality/linux?hours=168
   ```
   - Look for degrading silhouette scores
   - Review recommendations

2. **Check Drift Signals**
   ```bash
   curl http://localhost:8000/api/v1/metrics/drift/linux?hours=168
   ```
   - Identify pattern changes
   - Correlate with deployments

3. **Adjust Thresholds**
   - If silhouette < 0.3: reduce `CLUSTER_DISTANCE_THRESHOLD`
   - If new_cluster_rate > 15%: investigate or re-cluster

### Monthly

1. **Batch Re-clustering**
   ```bash
   # Run clustering script to rebuild prototypes
   python scripts/cluster_templates.py --os linux --threshold 0.18
   ```

2. **Cost Analysis**
   - Calculate monthly LLM spend
   - Evaluate caching opportunities
   - Consider prompt optimization

## Improvement Strategies

### 1. Poor Clustering Quality (Silhouette < 0.3)

**Diagnosis:**
```bash
curl http://localhost:8000/api/v1/metrics/quality/linux
```

**Actions:**
1. Reduce `CLUSTER_DISTANCE_THRESHOLD` from 0.2 to 0.15
2. Increase `CLUSTER_MIN_SIZE` from 5 to 10
3. Run batch re-clustering with new parameters

### 2. High LLM Costs

**Diagnosis:**
```bash
curl http://localhost:8000/api/v1/metrics/llm-usage?hours=168
```

**Actions:**
1. Implement semantic caching (hash similar prompts)
2. Reduce prompt length by limiting examples
3. Only process clusters with high anomaly scores
4. Batch multiple small clusters in one LLM call

### 3. High Drift Rate

**Diagnosis:**
```bash
curl http://localhost:8000/api/v1/metrics/drift/windows?hours=48
```

**Actions:**
1. Investigate recent system changes or deployments
2. Check if new log sources were added
3. Run batch re-clustering to reorganize
4. Update templates with new patterns

### 4. Low LLM Confidence

**Diagnosis:**
- Average confidence < 0.6 in hourly breakdown

**Actions:**
1. Add more template examples to prompts
2. Increase k-neighbors for LLM context
3. Switch to a more capable model (e.g., gpt-4)
4. Provide domain-specific examples

## Data Retention

- **Metrics:** 7 days in Redis
- **Alerts:** 24 hours in Redis streams
- **Aggregated stats:** 7 days in Redis

To extend retention, export metrics to a time-series database (Prometheus, InfluxDB).

## Troubleshooting

### No Metrics Appearing

1. Check if observability is enabled:
   ```bash
   curl http://localhost:8000/api/v1/health/health
   ```

2. Verify Redis connection:
   ```bash
   redis-cli -h localhost -p 6379 KEYS "cluster_metrics:*"
   ```

3. Check logs for metrics aggregator:
   ```bash
   docker logs <container> | grep "metrics aggregator"
   ```

### Alerts Not Triggering

1. Verify threshold configuration in `.env`
2. Check alerts stream:
   ```bash
   redis-cli -h localhost -p 6379 XREAD COUNT 10 STREAMS alerts 0
   ```

3. Review aggregator logs for errors

### High Memory Usage

Metrics are stored in Redis with TTL. If memory grows:
1. Reduce retention period
2. Decrease aggregation frequency
3. Export metrics to external storage

## Next Steps

1. **Integrate with Monitoring** - Send metrics to Grafana/Prometheus
2. **Implement Caching** - Add semantic caching for LLM calls
3. **Automate Tuning** - Create scripts to adjust thresholds based on metrics
4. **Add Dashboards** - Build web UI for visualizing trends
5. **Correlation Analysis** - Link cluster drift to incidents

## Example Dashboard Queries

### Grafana/Prometheus Integration

```python
# Export metrics to Prometheus format
@app.get("/metrics")
async def prometheus_metrics():
    redis_client = aioredis.from_url(settings.REDIS_URL)
    tracker = ClusterMetricsTracker(redis_client)
    
    metrics = []
    for os_name in ["linux", "macos", "windows"]:
        quality = await tracker.get_quality_metrics(os_name, hours=1)
        if quality:
            latest = quality[0]
            metrics.append(
                f'cluster_silhouette_score{{os="{os_name}"}} {latest["silhouette_score"]}'
            )
    
    await redis_client.close()
    return "\n".join(metrics)
```

## Support

For issues or questions about clustering observability:
1. Check logs: `docker logs <container> | grep cluster_metrics`
2. Review Redis keys: `redis-cli KEYS "cluster_metrics:*"`
3. Test API endpoints with sample queries above



