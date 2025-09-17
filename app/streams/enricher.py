import asyncio
from contextlib import suppress
from typing import Any, Dict, List

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.services.llm_service import classify_failure, generate_hypothesis, classify_issue
from app.services.chroma_service import ChromaClientProvider, collection_name_for_os
from app.core.config import settings as global_settings


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


def _logs_collection_name(os_name: str) -> str:
    suffix = os_name.strip().lower()
    if suffix in {"mac", "macos", "osx"}:
        suffix = "macos"
    elif suffix in {"windows", "win"}:
        suffix = "windows"
    elif suffix not in {"linux"}:
        suffix = suffix or "unknown"
    return f"{settings.CHROMA_LOG_COLLECTION_PREFIX}{suffix}"


async def _retrieve_logs_by_queries(os_name: str, queries: List[str], k_per_query: int = 5) -> List[Dict[str, Any]]:
    if not queries:
        return []
    provider = ChromaClientProvider()
    collection = provider.get_or_create_collection(_logs_collection_name(os_name))
    out: List[Dict[str, Any]] = []
    for q in queries[:3]:
        result = collection.query(query_texts=[q], n_results=k_per_query, include=["documents", "metadatas", "distances"])
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        for i in range(len(ids)):
            out.append({
                "id": ids[i],
                "document": docs[i] if i < len(docs) else "",
                "distance": dists[i] if i < len(dists) else None,
                "metadata": metas[i] if i < len(metas) else {},
            })
    return out


async def run_enricher():
    """Consume issues_candidates stream, enrich via LLM with HYDE, and write to alerts stream."""
    group = "issues_enrichers"
    consumer = "enricher_1"
    try:
        await redis.xgroup_create(settings.ISSUES_CANDIDATES_STREAM, group, id="$", mkstream=True)
    except Exception:
        pass

    while True:
        response = await redis.xreadgroup(group, consumer, {settings.ISSUES_CANDIDATES_STREAM: ">"}, count=5, block=1000)
        if not response:
            continue
        for _, messages in response:
            for msg_id, data in messages:
                os_name = data.get("os") or "unknown"
                templated_summary = data.get("templated_summary") or ""
                logs: List[Dict[str, Any]] = data.get("logs") or []

                # neighbors from templates for coarse context
                neighbors = await _retrieve_neighbors(os_name, templated_summary or (logs[0].get("templated") if logs else ""), k=8)
                # HYDE queries and retrieval from logs_<os>
                queries = generate_hypothesis(os_name, templated_summary, logs, num_queries=3)
                retrieved = await _retrieve_logs_by_queries(os_name, queries, k_per_query=5)
                retrieved_logs = [{
                    "templated": item.get("document", ""),
                    "raw": (item.get("metadata") or {}).get("raw", ""),
                } for item in retrieved]

                result = classify_issue(os_name, logs, neighbors, retrieved_logs)
                await redis.xadd(settings.ALERTS_STREAM, {
                    "type": "issue",
                    "os": os_name,
                    "issue_key": data.get("issue_key", ""),
                    "result": str(result),
                })
                await redis.xack(settings.ISSUES_CANDIDATES_STREAM, group, msg_id)


if __name__ == "__main__":
    asyncio.run(run_enricher())


