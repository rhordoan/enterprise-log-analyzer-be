import asyncio
import json
import logging
from collections import defaultdict
from typing import Any, Dict, List

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.services.chroma_service import ChromaClientProvider, collection_name_for_os
from app.services.clustering_service import (
    _logs_collection_name,
    _single_pass_cluster,
    build_prototypes,
    upsert_prototypes,
)
from app.services.embedding import get_embedding_model


LOG = logging.getLogger(__name__)
settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def improve_prototypes():
    """
    Analyzes alerts with "correct" feedback to generate new prototypes.
    """
    LOG.info("Starting prototype improvement process...")
    provider = ChromaClientProvider()

    # Get all alert IDs marked as 'correct'
    alert_ids = await redis.smembers(settings.ALERTS_FEEDBACK_CORRECT_SET)
    if not alert_ids:
        LOG.info("No new 'correct' feedback found. Exiting.")
        return

    LOG.info(f"Found {len(alert_ids)} alerts with 'correct' feedback.")

    # Fetch log_ids for each alert
    pipe = redis.pipeline()
    for alert_id in alert_ids:
        pipe.hget(f"alert:{alert_id}", "log_ids")
    
    log_ids_json = await pipe.execute()

    # Group log IDs by OS
    os_log_ids = defaultdict(list)
    
    pipe = redis.pipeline()
    for i, alert_id in enumerate(alert_ids):
        if log_ids_json[i]:
            try:
                # Need to find the OS for each alert to query the correct collection.
                pipe.hget(f"alert:{alert_id}", "os")
            except json.JSONDecodeError:
                LOG.warning(f"Failed to parse log_ids for alert {alert_id}")
    
    os_names = await pipe.execute()
    
    for i, alert_id in enumerate(alert_ids):
        if log_ids_json[i] and os_names[i]:
            try:
                ids = json.loads(log_ids_json[i])
                os_log_ids[os_names[i]].extend(ids)
            except json.JSONDecodeError:
                pass


    if not os_log_ids:
        LOG.info("No processable log IDs found in feedback alerts.")
        return

    # Process logs for each OS to generate new prototypes
    for os_name, log_ids in os_log_ids.items():
        if not log_ids:
            continue
        
        LOG.info(f"Processing {len(log_ids)} logs for OS: {os_name}")
        
        # Fetch embeddings and documents from ChromaDB
        try:
            log_collection_name = _logs_collection_name(os_name)
            collection = provider.get_or_create_collection(log_collection_name)
            
            # ChromaDB get can be slow for many IDs. Let's do it in batches.
            batch_size = 100
            all_results = {"ids": [], "embeddings": [], "documents": []}
            
            for i in range(0, len(log_ids), batch_size):
                batch_ids = log_ids[i:i + batch_size]
                results = collection.get(ids=batch_ids, include=["embeddings", "documents"])
                all_results["ids"].extend(results["ids"])
                all_results["embeddings"].extend(results["embeddings"])
                all_results["documents"].extend(results["documents"])
            
            if not all_results["embeddings"]:
                LOG.warning(f"No embeddings found for any log_ids for OS: {os_name}")
                continue

            # Cluster the retrieved logs
            clusters, centroids = _single_pass_cluster(
                all_results["embeddings"],
                threshold=settings.CLUSTER_DISTANCE_THRESHOLD,
                min_size=max(1, settings.CLUSTER_MIN_SIZE // 2),  # Lower min_size for refinement
            )

            if not clusters:
                LOG.info(f"No new clusters formed for OS: {os_name}")
                continue

            # Build and upsert new prototypes
            new_prototypes = build_prototypes(
                ids=all_results["ids"],
                documents=all_results["documents"],
                embeddings=all_results["embeddings"],
                clusters=clusters,
                centroids=centroids,
            )

            count = upsert_prototypes(os_name, provider, new_prototypes)
            LOG.info(f"Upserted {count} new or updated prototypes for OS: {os_name}")

        except Exception as e:
            LOG.error(f"Failed to process logs and generate prototypes for OS {os_name}: {e}")

    # Clear the set of processed alert IDs
    LOG.info(f"Clearing {len(alert_ids)} processed alert IDs from the feedback set.")
    await redis.srem(settings.ALERTS_FEEDBACK_CORRECT_SET, *alert_ids)

    LOG.info("Prototype improvement process finished.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(improve_prototypes())
