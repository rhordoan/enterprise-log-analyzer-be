# LogBERT Migration Guide

## Overview

This guide explains how to switch from **template-based clustering** (syntactic) to **LogBERT semantic clustering** (failure mode-based).

## What's the Difference?

### Template-Based (Current Default)
- **How it works:** Replaces variables with `<*>`, embeds the template
- **Groups by:** Text pattern similarity
- **Example:**
  ```
  "Temperature sensor CPU1 reading 45°C"   → Template: "Temperature sensor <*> reading <*>°C"
  "Temperature sensor CPU1 reading 95°C"   → Template: "Temperature sensor <*> reading <*>°C"
  ```
  These get the **same cluster** (identical template)

- **Problem:** Can't distinguish normal from critical:
  ```
  "Fan speed 1200 RPM" (normal)
  "Fan speed 0 RPM"    (critical failure)
  ```
  Both → same cluster (identical template), even though one is a failure

### LogBERT Semantic (New)
- **How it works:** Embeds the raw log using BERT (understands semantics)
- **Groups by:** Failure meaning, not just syntax
- **Example:**
  ```
  "Temperature sensor CPU1 reading 95°C"
  "Critical: Thermal threshold exceeded"
  "System shutdown due to overheating"
  ```
  All cluster together as **"thermal failure"** despite different syntax

- **Benefit:** Distinguishes failure modes:
  ```
  "Fan speed 0 RPM"       → "cooling failure" cluster
  "Fan speed 1200 RPM"    → "normal operation" cluster
  ```

## When to Use LogBERT

✅ **Use LogBERT if:**
- You have **hardware logs** (Redfish, SNMP, DCIM, Dell OME)
- Logs have **varying numeric values** (temps, RPMs, voltages)
- You want to cluster by **failure type**, not text pattern
- Your new cluster rate is **>30%** (too many unique clusters)

❌ **Stick with templates if:**
- You have **application logs** with consistent formats
- Performance is critical (LogBERT is slower)
- You don't have GPU and process >1000 logs/sec
- Current clustering works well (new cluster rate <10%)

## Migration Steps

### 1. Update Dependencies

Already done in `pyproject.toml`:
```bash
poetry install
# This installs transformers and torch
```

### 2. Update Environment Variables

Add to your `.env` file:

```bash
# Switch to LogBERT
EMBEDDING_PROVIDER=logbert

# Model selection (choose one):
LOGBERT_MODEL_NAME=bert-base-uncased          # Default, good for general logs
# LOGBERT_MODEL_NAME=microsoft/codebert-base  # Better for code/stack traces
# LOGBERT_MODEL_NAME=roberta-base             # Alternative, similar performance

# Device (CPU or GPU)
LOGBERT_DEVICE=cpu  # Use "cuda" if you have NVIDIA GPU

# Enable raw log embedding (required for semantic clustering)
LOGBERT_USE_RAW_LOGS=true

# Adjust threshold (LogBERT distances are different from template distances)
ONLINE_CLUSTER_DISTANCE_THRESHOLD=0.35  # Start here, tune based on metrics
```

### 3. Clear Existing Clusters (Optional but Recommended)

If you have existing clusters from template-based clustering, they won't be compatible with LogBERT embeddings.

**Option A: Fresh start** (recommended for testing):
```bash
# Stop the app
docker compose down

# Delete ChromaDB data
rm -rf .chroma

# Restart
docker compose up -d
```

**Option B: Keep existing data** (side-by-side):
ChromaDB automatically namespaces collections by embedding function, so old and new clusters won't interfere. The system will create new `proto_*__logbert::bert-base-uncased` collections.

### 4. Restart the Application

```bash
docker compose restart app
# Or if running locally:
poetry run python -m app.run --with-producer --with-enricher --reload
```

### 5. Monitor Cluster Metrics

Watch for improved clustering:

```bash
# Check new cluster rate (should drop from 70% → 10-20%)
curl http://localhost:8000/api/v1/metrics/drift/unknown?hours=24

# Expected output:
{
  "new_clusters": 15,
  "total_assignments": 150,
  "new_cluster_percentage": 10.0  // Much better!
}
```

### 6. Tune the Distance Threshold

LogBERT uses cosine distance (0.0 = identical, 2.0 = opposite). Typical ranges:

- `0.25-0.30`: Very strict (few large clusters, high precision)
- `0.35-0.40`: Moderate (balanced, **recommended starting point**)
- `0.45-0.50`: Permissive (many small clusters, higher recall)

**How to tune:**

1. Start with `ONLINE_CLUSTER_DISTANCE_THRESHOLD=0.35`
2. Monitor metrics for 1-2 hours
3. If new cluster rate still >20%, increase to `0.40`
4. If new cluster rate <5% but clusters feel too broad, decrease to `0.30`

Check distance distribution:
```bash
curl http://localhost:8000/api/v1/metrics/clusters/unknown?hours=24
```

Look for the `distances` field to see typical inter-cluster distances.

## Performance Considerations

### CPU vs GPU

| Device | Logs/sec | Latency/log | Cost |
|--------|----------|-------------|------|
| CPU (default) | 20-100 | 10-50ms | Free |
| GPU (CUDA) | 200-1000+ | 2-5ms | GPU required |

**When to use GPU:**
- Processing >500 logs/sec
- Real-time clustering required
- Have NVIDIA GPU available

**To enable GPU:**
```bash
LOGBERT_DEVICE=cuda  # In .env
```

**GPU setup (Docker):**
```yaml
# docker-compose.yml
services:
  app:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

### Model Size Trade-offs

| Model | Size | Speed | Quality |
|-------|------|-------|---------|
| `distilbert-base-uncased` | 66MB | Fast | Good |
| `bert-base-uncased` | 110MB | Medium | Better (default) |
| `roberta-base` | 125MB | Medium | Better |
| `bert-large-uncased` | 340MB | Slow | Best |

For production with high volume, use `distilbert-base-uncased`:
```bash
LOGBERT_MODEL_NAME=distilbert-base-uncased
```

## Troubleshooting

### Issue: "Model download is slow"

**Solution:** Pre-download the model:
```bash
poetry run python -c "from transformers import AutoModel; AutoModel.from_pretrained('bert-base-uncased')"
```

Models are cached in `~/.cache/huggingface/`.

### Issue: "Out of memory"

**Symptoms:**
```
RuntimeError: CUDA out of memory
```

**Solutions:**
1. Use smaller model: `distilbert-base-uncased`
2. Switch to CPU: `LOGBERT_DEVICE=cpu`
3. Reduce batch size (modify `LogBERTEmbeddingFunction` to process fewer logs at once)

### Issue: "Clustering is too slow"

**Measured by:** Check logs for "consumer: processing batch" latency

**Solutions:**
1. Enable GPU: `LOGBERT_DEVICE=cuda`
2. Use smaller model: `distilbert-base-uncased`
3. Increase threshold (fewer cluster lookups): `ONLINE_CLUSTER_DISTANCE_THRESHOLD=0.45`
4. Disable clustering for low-priority sources

### Issue: "Still too many new clusters (>30%)"

**Diagnosis:**
```bash
curl http://localhost:8000/api/v1/metrics/drift/unknown?hours=1
```

**Solutions:**
1. **Increase threshold:** Try `0.40` → `0.45` → `0.50`
2. **Check log diversity:** Are logs actually unique? (e.g., every log has unique UUID)
3. **Pre-process logs:** Strip timestamps, UUIDs, IP addresses before embedding
4. **Use hybrid approach:** Combine template pre-filtering + LogBERT for edge cases

### Issue: "Clusters are too broad (false positives)"

**Symptoms:** Unrelated logs in same cluster

**Solutions:**
1. **Decrease threshold:** Try `0.35` → `0.30` → `0.25`
2. **Use larger model:** `bert-large-uncased` or `roberta-base`
3. **Fine-tune BERT:** Train on your specific log data (advanced)

## Advanced: Fine-Tuning BERT on Your Logs

For best results, fine-tune BERT on your log data:

### 1. Prepare Training Data

Create pairs of (log, label):
```json
[
  {"text": "Temperature sensor CPU1 reading 95°C", "label": "thermal_critical"},
  {"text": "Critical: Thermal threshold exceeded", "label": "thermal_critical"},
  {"text": "Fan speed 0 RPM", "label": "cooling_failure"},
  {"text": "Cooling fan failure detected", "label": "cooling_failure"}
]
```

### 2. Fine-Tune

```python
from transformers import AutoTokenizer, AutoModelForSequenceClassification, Trainer

model = AutoModelForSequenceClassification.from_pretrained("bert-base-uncased", num_labels=num_labels)
# ... train model on your log pairs ...
model.save_pretrained("./my-log-bert")
```

### 3. Use Custom Model

```bash
LOGBERT_MODEL_NAME=./my-log-bert  # Path to your fine-tuned model
```

## Rollback

To revert to template-based clustering:

```bash
# In .env:
EMBEDDING_PROVIDER=ollama  # Or openai, sentence-transformers
# Remove LogBERT settings

# Restart
docker compose restart app
```

Old template-based clusters will still be in ChromaDB (under different collection names).

## FAQ

**Q: Can I use both template and LogBERT clustering?**

A: Not simultaneously, but ChromaDB stores them in separate collections, so you can switch back and forth without data loss.

**Q: Will LogBERT work for application logs (not hardware)?**

A: Yes, but benefit is smaller. LogBERT excels at hardware logs with numeric values. For app logs, template clustering often works well.

**Q: How much does LogBERT cost to run?**

A: Free (runs locally). No API calls. Just requires CPU/GPU compute.

**Q: Can I use a GPU in the cloud (AWS, OCI, GCP)?**

A: Yes! Use GPU-enabled instances (e.g., OCI VM.GPU.A10.1). See `OCI_DEPLOYMENT_COST_ESTIMATE.md` for pricing.

**Q: What if I don't have labeled data for fine-tuning?**

A: Start with `bert-base-uncased` pretrained. It works well out-of-the-box for most logs. Fine-tuning is optional.

## Next Steps

1. ✅ Enable LogBERT (follow steps above)
2. ✅ Monitor cluster metrics for 24 hours
3. ✅ Tune threshold based on metrics
4. ⏳ (Optional) Fine-tune BERT on your log data
5. ⏳ (Optional) Enable GPU for production workloads

For questions, see the main README or open an issue.
















