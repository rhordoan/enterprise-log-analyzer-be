import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, Dict, List

import redis.asyncio as aioredis

from fastapi import FastAPI
from app.core.config import get_settings
from app.services.llm_service import classify_failure, generate_hypothesis, classify_issue
from app.services.chroma_service import ChromaClientProvider, collection_name_for_os
from app.core.config import settings as global_settings
import threading


settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
LOG = logging.getLogger(__name__)
_provider: ChromaClientProvider | None = None


def _get_provider() -> ChromaClientProvider:
    global _provider
    if _provider is None:
        _provider = ChromaClientProvider()
    return _provider


async def _retrieve_neighbors(os_name: str, templated: str, k: int = 5) -> List[Dict[str, Any]]:
    provider = _get_provider()
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
    provider = _get_provider()
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
        try:
            response = await redis.xreadgroup(group, consumer, {settings.ISSUES_CANDIDATES_STREAM: ">"}, count=5, block=1000)
        except Exception as exc:
            LOG.info("enricher read failed err=%s", exc)
            await asyncio.sleep(1)
            continue
        if not response:
            continue
        for _, messages in response:
            for msg_id, data in messages:
                try:
                    os_name = data.get("os") or "unknown"
                    templated_summary = data.get("templated_summary") or ""
                    raw_logs = data.get("logs")
                    if isinstance(raw_logs, str):
                        try:
                            logs: List[Dict[str, Any]] = json.loads(raw_logs)
                        except Exception:
                            logs = []
                    else:
                        logs = raw_logs or []

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
                    # Normalize fields for easier consumption on the UI
                    is_hw = bool(result.get("is_hardware_failure"))
                    failure_type = str(result.get("failure_type", ""))
                    confidence = result.get("confidence")
                    log_ids = [log.get("id") for log in logs if log.get("id")]
                    payload = {
                        "type": "issue",
                        "os": os_name,
                        "issue_key": data.get("issue_key", ""),
                        "is_hardware_failure": str(is_hw).lower(),  # streams are strings
                        "failure_type": failure_type,
                        "confidence": str(confidence) if confidence is not None else "",
                        "result": json.dumps(result),
                        "log_ids": json.dumps(log_ids),
                    }
                    entry_id = await redis.xadd(settings.ALERTS_STREAM, payload)
                    # Mirror alert into a hash with a TTL for ~24h visibility; allow persisting later
                    try:
                        key = f"alert:{entry_id}"
                        # Store fields as strings for consistency
                        to_store = {**payload, "id": entry_id}
                        await redis.hset(key, mapping=to_store)
                        await redis.expire(key, int(settings.ALERTS_TTL_SEC))
                    except Exception as e:
                        LOG.info("failed to store alert hash id=%s err=%s", entry_id, e)
                except Exception as exc:
                    LOG.info("enricher processing failed id=%s err=%s", msg_id, exc)
                finally:
                    try:
                        await redis.xack(settings.ISSUES_CANDIDATES_STREAM, group, msg_id)
                    except Exception as exc:
                        LOG.info("enricher ack failed id=%s err=%s", msg_id, exc)


if __name__ == "__main__":
    asyncio.run(run_enricher())


def attach_enricher(app: FastAPI):
    async def _run_forever():
        backoff = 1.0
        while True:
            try:
                await run_enricher()
            except Exception as exc:
                LOG.info("enricher crashed err=%s; restarting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    @app.on_event("startup")
    async def startup_event():
        LOG.info("starting enricher in dedicated thread")
        loop = asyncio.new_event_loop()

        def _runner():
            asyncio.set_event_loop(loop)
            loop.create_task(_run_forever())
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="enricher-thread", daemon=True)
        thread.start()
        app.state.enricher_loop = loop
        app.state.enricher_thread = thread

    @app.on_event("shutdown")
    async def shutdown_event():
        LOG.info("stopping enricher thread")
        loop = getattr(app.state, "enricher_loop", None)
        thread = getattr(app.state, "enricher_thread", None)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)


