#!/usr/bin/env python3
"""
Test LogBERT logging
"""
import logging
import sys
import os

# Add the app directory to Python path
sys.path.insert(0, os.path.dirname(__file__))

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    stream=sys.stdout
)

print("Testing LogBERT logging...", flush=True)

try:
    from app.services.embedding import LogBERTEmbeddingFunction
    print("LogBERT imported successfully", flush=True)

    print("Initializing LogBERT...", flush=True)
    emb_func = LogBERTEmbeddingFunction()
    print("LogBERT initialized", flush=True)

    print("Testing embedding with logging...", flush=True)
    result = emb_func(['Test log message for logging'])
    print(f"Embedding generated: {len(result)} vectors, dim {len(result[0])}", flush=True)

except Exception as e:
    print(f"ERROR: {e}", flush=True)
    import traceback
    traceback.print_exc()














