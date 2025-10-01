from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

import redis.asyncio as aioredis

from fastapi import FastAPI
from app.core.config import get_settings
from app.services.chroma_service import ChromaClientProvider, collection_name_for_os
from app.services.llm_service import classify_cluster, generate_hypothesis
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


def _suffix_for_os(os_name: str) -> str:
    key = (os_name or "").strip().lower()
    if key in {"mac", "macos", "osx"}:
        return "macos"
    if key in {"linux"}:
        return "linux"
    if key in {"windows", "win"}:
        return "windows"
    return key or "unknown"


def _logs_collection_name(os_name: str) -> str:
    return f"{settings.CHROMA_LOG_COLLECTION_PREFIX}{_suffix_for_os(os_name)}"


def _proto_collection_name(os_name: str) -> str:
    return f"{settings.CHROMA_PROTO_COLLECTION_PREFIX}{_suffix_for_os(os_name)}"


def _get_prototype(os_name: str, cluster_id: str) -> tuple[list[float] | None, str, Dict[str, Any]]:
    collection = _get_provider().get_or_create_collection(_proto_collection_name(os_name))
    data = collection.get(ids=[cluster_id], include=["embeddings", "documents", "metadatas"]) or {}
    embs = data.get("embeddings") or []
    docs = data.get("documents") or []
    metas = data.get("metadatas") or []
    centroid = embs[0] if embs else None
    medoid_doc = docs[0] if docs else ""
    meta = metas[0] if metas else {}
    return centroid, medoid_doc, meta


async def run_cluster_enricher() -> None:
    group = "clusters_enrichers"
    consumer = "cluster_enricher_1"
    try:
        await redis.xgroup_create(settings.CLUSTERS_CANDIDATES_STREAM, group, id="$", mkstream=True)
    except Exception:
        pass

    while True:
        try:
            response = await redis.xreadgroup(group, consumer, {settings.CLUSTERS_CANDIDATES_STREAM: ">"}, count=5, block=1000)
        except Exception as exc:
            LOG.info("cluster enricher read failed err=%s", exc)
            await asyncio.sleep(1)
            continue
        if not response:
            continue
        for _, messages in response:
            for msg_id, data in messages:
                try:
                    os_name = data.get("os") or "unknown"
                    cluster_id = data.get("cluster_id") or ""
                    centroid, medoid_doc, proto_meta = _get_prototype(os_name, cluster_id)

                    # neighbors from templates via centroid
                    neighbors: List[Dict[str, Any]] = []
                    if centroid:
                        tcoll = _get_provider().get_or_create_collection(collection_name_for_os(os_name))
                        q = tcoll.query(query_embeddings=[centroid], n_results=8, include=["documents", "metadatas", "distances"]) or {}
                        ids = (q.get("ids") or [[]])[0]
                        docs = (q.get("documents") or [[]])[0]
                        dists = (q.get("distances") or [[]])[0]
                        metas = (q.get("metadatas") or [[]])[0]
                        for i in range(len(ids)):
                            neighbors.append({
                                "id": ids[i],
                                "document": docs[i] if i < len(docs) else "",
                                "distance": dists[i] if i < len(dists) else None,
                                "metadata": metas[i] if i < len(metas) else {},
                            })

                    # HYDE queries using medoid
                    seed_logs = [{"templated": medoid_doc}] if medoid_doc else []
                    queries = generate_hypothesis(os_name, medoid_doc, seed_logs, num_queries=3)

                    # retrieve logs within same cluster via where filter
                    retrieved: List[Dict[str, Any]] = []
                    lcoll = _get_provider().get_or_create_collection(_logs_collection_name(os_name))
                    for qtxt in (queries or [medoid_doc] or []):
                        try:
                            res = lcoll.query(query_texts=[qtxt], where={"cluster_id": cluster_id}, n_results=10, include=["documents", "metadatas", "distances"]) or {}
                        except Exception:
                            res = {}
                        ids = (res.get("ids") or [[]])[0]
                        docs = (res.get("documents") or [[]])[0]
                        metas = (res.get("metadatas") or [[]])[0]
                        for i in range(len(ids)):
                            retrieved.append({
                                "id": ids[i],
                                "templated": docs[i] if i < len(docs) else "",
                                "raw": (metas[i] or {}).get("raw", ""),
                            })

                    result = classify_cluster(os_name, cluster_id, medoid_doc, neighbors, retrieved)
                    payload = {
                        "type": "cluster",
                        "os": os_name,
                        "cluster_id": cluster_id,
                        "failure_type": result.get("failure_type", ""),
                        "confidence": str(result.get("confidence") or ""),
                        "result": json.dumps(result),
                    }
                    await redis.xadd(settings.ALERTS_STREAM, payload)

                    # Update prototype metadata with learned label/solution
                    try:
                        pcoll = _get_provider().get_or_create_collection(_proto_collection_name(os_name))
                        meta = dict(proto_meta or {})
                        meta["label"] = result.get("failure_type", meta.get("label", "unknown"))
                        meta["rationale"] = "llm_cluster"
                        if result.get("recommendation"):
                            meta["solution"] = result.get("recommendation")
                        pcoll.update(ids=[cluster_id], metadatas=[meta])
                    except Exception:
                        pass
                except Exception as exc:
                    LOG.info("cluster enricher processing failed id=%s err=%s", msg_id, exc)
                finally:
                    try:
                        await redis.xack(settings.CLUSTERS_CANDIDATES_STREAM, group, msg_id)
                    except Exception:
                        pass


def attach_cluster_enricher(app: FastAPI):
    async def _run_forever():
        backoff = 1.0
        while True:
            try:
                await run_cluster_enricher()
            except Exception as exc:
                LOG.info("cluster enricher crashed err=%s; restarting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    @app.on_event("startup")
    async def startup_event():
        if not settings.ENABLE_CLUSTER_ENRICHER:
            return
        LOG.info("starting cluster enricher in dedicated thread")
        loop = asyncio.new_event_loop()

        def _runner():
            asyncio.set_event_loop(loop)
            loop.create_task(_run_forever())
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="cluster-enricher-thread", daemon=True)
        thread.start()
        app.state.cluster_enricher_loop = loop
        app.state.cluster_enricher_thread = thread

    @app.on_event("shutdown")
    async def shutdown_event():
        loop = getattr(app.state, "cluster_enricher_loop", None)
        thread = getattr(app.state, "cluster_enricher_thread", None)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)






