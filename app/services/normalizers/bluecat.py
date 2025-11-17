from __future__ import annotations

from typing import Any, Dict, List

from app.services.metrics_normalization import MetricPoint, register_normalizer, now_nano


@register_normalizer("bluecat")
def normalize_bluecat(_: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> List[MetricPoint]:
    ts = now_nano()
    resource = {"vendor": "bluecat"}
    out: List[MetricPoint] = []
    sev = str(payload.get("severity") or payload.get("level") or "").lower()
    sev_map = {"info": 0, "warning": 1, "minor": 1, "major": 2, "critical": 3, "error": 3}
    out.append({
        "name": "bluecat.event.severity",
        "type": "gauge",
        "value": float(sev_map.get(sev, 0)),
        "unit": None,
        "time_unix_nano": ts,
        "resource": resource,
        "attributes": {"category": str(payload.get("category") or "")},
    })
    return out





