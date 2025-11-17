#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

print("Quick test starting...", flush=True)

from app.services.embedding import LogBERTEmbeddingFunction
print("LogBERT imported", flush=True)

emb = LogBERTEmbeddingFunction()
print("LogBERT initialized", flush=True)

print("Starting embedding generation...", flush=True)
result = emb(['test log message'])
print(f"Done! Result: {len(result)} vectors", flush=True)














