#!/usr/bin/env python3
"""
Debug why embedding commands don't produce output
"""
import sys
import os
import time

# Add the app directory to Python path
sys.path.insert(0, os.path.dirname(__file__))

print("Starting debug at", time.time(), flush=True)

try:
    print("Step 1: Importing config", flush=True)
    from app.core.config import settings
    print("Config imported successfully", flush=True)

    print("Step 2: Checking embedding provider", flush=True)
    print(f"EMBEDDING_PROVIDER: {settings.EMBEDDING_PROVIDER}", flush=True)

    print("Step 3: Importing embedding module", flush=True)
    from app.services import embedding
    print("Embedding module imported", flush=True)

    print("Step 4: Testing LogBERT import", flush=True)
    from app.services.embedding import LogBERTEmbeddingFunction
    print("LogBERT class imported", flush=True)

    print("Step 5: Attempting LogBERT initialization", flush=True)
    start_time = time.time()
    emb_func = LogBERTEmbeddingFunction()
    end_time = time.time()
    print(".2f", flush=True)

    print("Step 6: Testing embedding generation", flush=True)
    result = emb_func(['test message'])
    print(f"Embedding generated: {len(result)} vectors", flush=True)

    print("SUCCESS: All steps completed", flush=True)

except Exception as e:
    print(f"ERROR at step: {e}", flush=True)
    import traceback
    traceback.print_exc()
    sys.exit(1)















