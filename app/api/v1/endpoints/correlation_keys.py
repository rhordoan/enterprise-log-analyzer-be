from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Query

from app.services.correlation_keys import compute_key_correlation


router = APIRouter()


@router.get("/correlation/keys", response_model=Dict[str, Any])
async def get_key_correlation(
    window_min: int = Query(60, ge=1, le=1440, description="Window in minutes (best-effort; sampling-based)"),
    limit: int = Query(2000, ge=10, le=10000, description="Limit of sampled events across OS collections"),
    keys: str = Query("device_ip,client_mac", description="Comma-separated keys to correlate on"),
) -> Dict[str, Any]:
    # Current implementation samples from embeddings store (no timestamp filter). Window argument is accepted for API stability.
    selected_keys = [k.strip() for k in keys.split(",") if k.strip()]
    return compute_key_correlation(selected_keys, limit=limit)





