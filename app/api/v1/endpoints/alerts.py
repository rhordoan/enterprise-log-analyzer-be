from typing import Any, Dict, List

import json
from fastapi import APIRouter, Query
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
    """List recent alerts from the alerts stream (most recent first)."""
    # Use XREVRANGE to get latest entries first
    entries = await redis.xrevrange(settings.ALERTS_STREAM, max="+", min="-", count=limit)
    out: List[Dict[str, Any]] = []
    for entry_id, fields in entries:
        result_obj = _parse_result(fields.get("result"))
        out.append({
            "id": entry_id,
            "type": fields.get("type", ""),
            "os": fields.get("os", ""),
            "issue_key": fields.get("issue_key", ""),
            "result": result_obj,
        })
    return out


