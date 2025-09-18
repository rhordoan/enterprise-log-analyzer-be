import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from typing import Any, Dict, List

import redis.asyncio as aioredis
from redis.exceptions import ResponseError
from fastapi import FastAPI

from app.core.config import get_settings
from app.services.chroma_service import ChromaClientProvider
from app.services.failure_rules import match_failure_signals
from app.services.prototype_router import nearest_prototype
from app.parsers.linux import parse_linux_line
from app.parsers.macos import parse_macos_line
from app.parsers.templating import render_templated_line

settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

STREAM_NAME = "logs"
GROUP_NAME = "log_consumers"
CONSUMER_NAME = "consumer_1"

_provider: ChromaClientProvider | None = None
LOG = logging.getLogger(__name__)


def _get_provider() -> ChromaClientProvider:
    global _provider
    if _provider is None:
        _provider = ChromaClientProvider()
    return _provider


def _os_from_source(source: str | None) -> str:
    if not source:
        return "unknown"
    s = source.lower()
    if "linux.log" in s:
        return "linux"
    if "mac.log" in s:
        return "macos"
    if "windows" in s:
        return "windows"
    return "unknown"


def _log_collection_name(os_name: str) -> str:
    return f"{settings.CHROMA_LOG_COLLECTION_PREFIX}{os_name or 'unknown'}"


def _parse_and_template(os_name: str, line: str) -> tuple[str, Dict[str, str]]:
    parsed: Dict[str, str] | None = None
    if os_name == "linux":
        parsed = parse_linux_line(0, line) or None
    elif os_name == "macos":
        parsed = parse_macos_line(0, line) or None
    if not parsed:
        templated = render_templated_line(component="unknown", pid=None, content=line)
        return templated, {"content": line, "component": "unknown"}
    templated = render_templated_line(
        component=parsed.get("component", ""),
        pid=parsed.get("PID"),
        content=parsed.get("content", ""),
    )
    return templated, parsed


async def consume_logs():
    """Consume new messages from Redis Stream and acknowledge them."""
    # create consumer group if not exists
    try:
        await redis.xgroup_create(STREAM_NAME, GROUP_NAME, id="$", mkstream=True)
        LOG.info("consumer group created stream=%s group=%s", STREAM_NAME, GROUP_NAME)
    except ResponseError as exc:
        LOG.info("consumer group exists stream=%s group=%s info=%s", STREAM_NAME, GROUP_NAME, exc)

    LOG.info("consumer ready and entering read loop stream=%s group=%s consumer=%s", STREAM_NAME, GROUP_NAME, CONSUMER_NAME)

    while True:
        try:
            response = await redis.xreadgroup(
                GROUP_NAME,
                CONSUMER_NAME,
                {STREAM_NAME: ">"},
                count=50,
                block=1000,
            )
        except Exception as exc:
            LOG.info("xreadgroup failed stream=%s group=%s consumer=%s err=%s", STREAM_NAME, GROUP_NAME, CONSUMER_NAME, exc)
            await asyncio.sleep(1)
            continue
        if not response:
            continue

        provider = _get_provider()
        # Accumulate per collection for batch upserts
        batched: dict[str, dict[str, List[Any]]] = defaultdict(lambda: {"ids": [], "documents": [], "metadatas": []})
        candidates: List[Dict[str, Any]] = []
        ack_ids: List[str] = []

        total_msgs = 0
        for _, messages in response:
            for msg_id, data in messages:
                try:
                    total_msgs += 1
                    source = data.get("source")
                    line = data.get("line") or ""
                    os_name = _os_from_source(source)
                    templated, parsed = _parse_and_template(os_name, line)

                    # route to logs_<os>
                    coll_name = _log_collection_name(os_name)
                    batched[coll_name]["ids"].append(msg_id)
                    batched[coll_name]["documents"].append(templated)
                    batched[coll_name]["metadatas"].append({
                        "os": os_name,
                        "source": source or "",
                        "raw": line,
                        **parsed,
                    })

                    # quick rule signal
                    rule = match_failure_signals(f"{templated} {line}")

                    # nearest prototype distance (guard failures)
                    distance = None
                    label = None
                    try:
                        nearest = nearest_prototype(os_name, templated, k=1)
                        distance = nearest[0]["distance"] if nearest else None
                        label = (nearest[0]["metadata"] or {}).get("label") if nearest else None
                    except Exception as exc:
                        LOG.info("prototype routing failed os=%s err=%s", os_name, exc)

                    should_candidate = False
                    if rule.get("has_signal"):
                        should_candidate = True
                    if distance is None or (isinstance(distance, (int, float)) and distance > settings.NEAREST_PROTO_THRESHOLD):
                        should_candidate = True

                    if should_candidate:
                        candidates.append({
                            "os": os_name,
                            "raw": line,
                            "templated": templated,
                            "rule_label": rule.get("label"),
                            "rule_score": rule.get("score"),
                            "nearest_distance": distance if distance is not None else "",
                            "nearest_label": label or "",
                        })
                except Exception as exc:
                    LOG.info("consumer message processing failed id=%s err=%s", msg_id, exc)
                finally:
                    ack_ids.append(msg_id)

        LOG.info("processing batch size=%d collections=%d candidates=%d", total_msgs, len(batched), len(candidates))
        # Perform upserts per collection
        for coll_name, payload in batched.items():
            try:
                collection = provider.get_or_create_collection(coll_name)
                if payload["ids"]:
                    collection.upsert(ids=payload["ids"], documents=payload["documents"], metadatas=payload["metadatas"])
                    LOG.info("upserted collection=%s count=%d", coll_name, len(payload["ids"]))
            except Exception as exc:
                LOG.info("upsert failed collection=%s err=%s", coll_name, exc)

        # Publish per-line candidates if enabled
        if settings.ENABLE_PER_LINE_CANDIDATES:
            for c in candidates:
                try:
                    await redis.xadd(settings.ALERTS_CANDIDATES_STREAM, c)
                except Exception as exc:
                    LOG.info("publish candidate failed stream=%s err=%s", settings.ALERTS_CANDIDATES_STREAM, exc)

        # Acknowledge after successful writes
        if ack_ids:
            try:
                await redis.xack(STREAM_NAME, GROUP_NAME, *ack_ids)
                LOG.info("acked messages count=%d", len(ack_ids))
            except Exception as exc:
                LOG.info("ack failed count=%d err=%s", len(ack_ids), exc)


def attach_consumer(app: FastAPI):
    async def _run_forever():
        backoff = 1.0
        while True:
            try:
                LOG.info("starting consumer stream=%s group=%s consumer=%s", STREAM_NAME, GROUP_NAME, CONSUMER_NAME)
                await consume_logs()
            except Exception as exc:
                LOG.info("consumer crashed err=%s; restarting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    @app.on_event("startup")
    async def startup_event():
        app.state.consumer_task = asyncio.create_task(_run_forever())

    @app.on_event("shutdown")
    async def shutdown_event():
        LOG.info("stopping consumer")
        app.state.consumer_task.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.consumer_task
