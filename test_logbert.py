#!/usr/bin/env python3
"""
Test LogBERT embedding similarity
"""
import math
import sys
import os

# Add the app directory to Python path
sys.path.insert(0, os.path.dirname(__file__))

print("Testing LogBERT similarity...", flush=True)

try:
    from app.services.embedding import LogBERTEmbeddingFunction
    print("LogBERT imported successfully", flush=True)

    # Create two similar log messages
    log1 = 'Failed to connect to database server at 192.168.1.100: connection timeout after 30 seconds'
    log2 = 'Database connection failed: timeout after 30 seconds when connecting to 192.168.1.100'

    print(f"Log 1: {log1}", flush=True)
    print(f"Log 2: {log2}", flush=True)

    print("Initializing LogBERT...", flush=True)
    emb_func = LogBERTEmbeddingFunction()
    print("LogBERT initialized", flush=True)

    print("Generating embeddings...", flush=True)
    emb1_list = emb_func([log1])
    emb2_list = emb_func([log2])

    emb1 = emb1_list[0]
    emb2 = emb2_list[0]

    print(f"Embedding dimensions: {len(emb1)}", flush=True)

    # Calculate cosine similarity
    dot_product = sum(x * y for x, y in zip(emb1, emb2))
    norm1 = math.sqrt(sum(x * x for x in emb1))
    norm2 = math.sqrt(sum(y * y for y in emb2))
    cosine_similarity = dot_product / (norm1 * norm2)

    distance = 1.0 - cosine_similarity

    print(f"Cosine similarity: {cosine_similarity:.4f}", flush=True)
    print(f"Distance: {distance:.4f}", flush=True)

    threshold = 0.35
    would_cluster = distance <= threshold

    print(f"Threshold: {threshold}", flush=True)
    print(f"Would cluster: {would_cluster}", flush=True)

    if would_cluster:
        print("✅ SUCCESS: Similar logs would be grouped together", flush=True)
    else:
        print("❌ ISSUE: Similar logs would be split into different clusters", flush=True)

except Exception as e:
    print(f"ERROR: {e}", flush=True)
    import traceback
    traceback.print_exc()





