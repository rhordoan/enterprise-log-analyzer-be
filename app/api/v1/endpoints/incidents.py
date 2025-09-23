from typing import Any, Dict, List

import json
from fastapi import APIRouter, Query
import redis.asyncio as aioredis

from app.core.config import get_settings


router = APIRouter()
settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)


@router.get("/")
async def list_incidents(limit: int = Query(100, ge=1, le=1000)) -> List[Dict[str, Any]]:
    """List incidents published by the issues aggregator from the Redis stream."""
    # Newest first
    entries = await redis.xrevrange(settings.ISSUES_CANDIDATES_STREAM, max="+", min="-", count=limit)
    out: List[Dict[str, Any]] = []
    for entry_id, fields in entries:
        # Extract millisecond timestamp from stream ID
        ts_ms_str = entry_id.split("-")[0]
        try:
            time_ms = int(ts_ms_str)
        except Exception:
            time_ms = 0

        logs_raw = fields.get("logs") or "[]"
        try:
            logs = json.loads(logs_raw)
        except Exception:
            logs = []

        out.append({
            "id": entry_id,
            "os": fields.get("os", ""),
            "issue_key": fields.get("issue_key", ""),
            "templated_summary": fields.get("templated_summary", ""),
            "logs": logs,
            "time_ms": time_ms,
        })
    return out

