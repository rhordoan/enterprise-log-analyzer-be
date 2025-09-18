from typing import Any, Dict, List

import json
import time
from fastapi import APIRouter, Query, HTTPException
import redis.asyncio as aioredis

from app.core.config import get_settings


router = APIRouter()
settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)


def _parse_result(raw: str | None) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        # fallback: attempt to coerce single quotes -> double quotes
        try:
            cleaned = raw.replace("'", '"')
            return json.loads(cleaned)
        except Exception:
            return {"raw": raw}


@router.get("/")
async def list_alerts(limit: int = Query(100, ge=1, le=1000)) -> List[Dict[str, Any]]:
    """List alerts from the last ALERTS_TTL_SEC and include any persisted ones."""
    now_ms = int(time.time() * 1000)
    min_id = f"{now_ms - (int(settings.ALERTS_TTL_SEC) * 1000)}-0"

    # Fetch persisted ids once
    try:
        persisted_ids = await redis.smembers(settings.ALERTS_PERSISTED_SET)
    except Exception:
        persisted_ids = set()

    # Fetch recent (within TTL) from stream, newest first (bounded)
    stream_entries = await redis.xrevrange(settings.ALERTS_STREAM, max="+", min=min_id, count=limit)

    seen_ids: set[str] = set()
    out: List[Dict[str, Any]] = []
    for entry_id, fields in stream_entries:
        seen_ids.add(entry_id)
        result_obj = _parse_result(fields.get("result"))
        out.append({
            "id": entry_id,
            "type": fields.get("type", ""),
            "os": fields.get("os", ""),
            "issue_key": fields.get("issue_key", ""),
            "result": result_obj,
            "persisted": (entry_id in persisted_ids),
        })

    # If we still need more, include older persisted alerts (outside TTL)
    remaining = max(0, limit - len(out))
    if remaining > 0 and persisted_ids:
        # Older persisted ids not already included; sort by id desc (time component)
        candidates = sorted([pid for pid in persisted_ids if pid not in seen_ids], reverse=True)
        to_fetch = candidates[:remaining]
        if to_fetch:
            pipe = redis.pipeline(transaction=False)
            for pid in to_fetch:
                pipe.hgetall(f"alert:{pid}")
            fetched = await pipe.execute()
            for pid, data in zip(to_fetch, fetched):
                if not data:
                    continue
                result_obj = _parse_result(data.get("result"))
                out.append({
                    "id": pid,
                    "type": data.get("type", ""),
                    "os": data.get("os", ""),
                    "issue_key": data.get("issue_key", ""),
                    "result": result_obj,
                    "persisted": True,
                })

    # Sort by id (time component) desc and cap to limit
    out.sort(key=lambda a: a.get("id", ""), reverse=True)
    return out[:limit]


@router.post("/{entry_id}/persist")
async def persist_alert(entry_id: str) -> Dict[str, Any]:
    """Persist an alert beyond TTL: remove hash expiry and mark persisted set."""
    key = f"alert:{entry_id}"
    exists = await redis.exists(key)
    if not exists:
        # Try to reconstruct from the stream entry if available
        entries = await redis.xrange(settings.ALERTS_STREAM, min=entry_id, max=entry_id, count=1)
        if not entries:
            raise HTTPException(status_code=404, detail="alert not found")
        _, fields = entries[0]
        to_store = {**fields, "id": entry_id}
        await redis.hset(key, mapping=to_store)
    # Remove TTL and mark persisted
    await redis.persist(key)
    await redis.sadd(settings.ALERTS_PERSISTED_SET, entry_id)
    return {"status": "ok", "id": entry_id}


@router.post("/{entry_id}/feedback")
async def add_feedback(entry_id: str, feedback: str = Query(..., pattern="^(correct|incorrect)$")) -> Dict[str, Any]:
    """Add feedback to an alert."""
    key = f"alert:{entry_id}"
    exists = await redis.exists(key)
    if not exists:
        raise HTTPException(status_code=404, detail="alert not found")

    pipe = redis.pipeline()
    pipe.hset(key, "feedback", feedback)
    if feedback == "correct":
        pipe.sadd(settings.ALERTS_FEEDBACK_CORRECT_SET, entry_id)
        pipe.srem(settings.ALERTS_FEEDBACK_INCORRECT_SET, entry_id)
    else:
        pipe.sadd(settings.ALERTS_FEEDBACK_INCORRECT_SET, entry_id)
        pipe.srem(settings.ALERTS_FEEDBACK_CORRECT_SET, entry_id)
    await pipe.execute()
    
    return {"status": "ok", "id": entry_id, "feedback": feedback}


