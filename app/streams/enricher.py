import asyncio
from contextlib import suppress
from typing import Any, Dict, List

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.services.llm_service import classify_failure
from app.services.chroma_service import ChromaClientProvider, collection_name_for_os


settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def _retrieve_neighbors(os_name: str, templated: str, k: int = 5) -> List[Dict[str, Any]]:
    provider = ChromaClientProvider()
    # Query templates first; could extend to logs_<os> as well
    collection = provider.get_or_create_collection(collection_name_for_os(os_name))
    result = collection.query(query_texts=[templated], n_results=k, include=["distances", "metadatas", "documents"])
    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    out: List[Dict[str, Any]] = []
    for i in range(len(ids)):
        out.append({
            "id": ids[i],
            "document": docs[i] if i < len(docs) else "",
            "distance": dists[i] if i < len(dists) else None,
            "metadata": metas[i] if i < len(metas) else {},
        })
    return out


async def run_enricher():
    """Consume alerts_candidates stream, enrich via LLM, and write to alerts stream."""
    group = "alerts_enrichers"
    consumer = "enricher_1"
    try:
        await redis.xgroup_create(settings.ALERTS_CANDIDATES_STREAM, group, id="$", mkstream=True)
    except Exception:
        pass

    while True:
        response = await redis.xreadgroup(group, consumer, {settings.ALERTS_CANDIDATES_STREAM: ">"}, count=10, block=1000)
        if not response:
            continue
        for _, messages in response:
            for msg_id, data in messages:
                os_name = data.get("os") or "unknown"
                raw = data.get("raw") or ""
                templated = data.get("templated") or raw
                neighbors = await _retrieve_neighbors(os_name, templated)
                result = classify_failure(os_name, raw, templated, neighbors)
                await redis.xadd(settings.ALERTS_STREAM, {"os": os_name, "raw": raw, "templated": templated, "result": str(result)})
                await redis.xack(settings.ALERTS_CANDIDATES_STREAM, group, msg_id)


if __name__ == "__main__":
    asyncio.run(run_enricher())


