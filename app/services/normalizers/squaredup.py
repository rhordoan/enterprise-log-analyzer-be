from __future__ import annotations

from typing import Any, Dict, List

from app.services.metrics_normalization import MetricPoint, register_normalizer, now_nano


@register_normalizer("squaredup")
def normalize_squaredup(_: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> List[MetricPoint]:
    # Payload from SquaredUp producer: {"type":"health|alert|dependency", ...}
    typ = str(payload.get("type") or "")
    t_nano = now_nano()
    resource = {"vendor": "squaredup"}
    out: List[MetricPoint] = []

    if typ == "health":
        # Map states to 0/1, keep detail as attributes
        state = str(payload.get("state") or payload.get("status") or "").lower()
        val = 1 if state in {"ok", "healthy", "green"} else 0
        out.append({
            "name": "squaredup.health.ok",
            "type": "gauge",
            "value": val,
            "unit": None,
            "time_unix_nano": t_nano,
            "resource": resource,
            "attributes": {"state": state, "name": str(payload.get("name") or "")},
        })
        return out

    if typ == "alert":
        sev = str(payload.get("severity") or "").lower()
        sev_map = {"info": 0, "warning": 1, "critical": 2, "error": 2}
        val = sev_map.get(sev, 0)
        out.append({
            "name": "squaredup.alert.severity",
            "type": "gauge",
            "value": val,
            "unit": None,
            "time_unix_nano": t_nano,
            "resource": resource,
            "attributes": {"id": str(payload.get("id") or ""), "title": str(payload.get("title") or ""), "severity": sev},
        })
        return out

    if typ == "dependency":
        out.append({
            "name": "squaredup.dependency.edge.count",
            "type": "sum",
            "value": 1,
            "unit": None,
            "time_unix_nano": t_nano,
            "resource": resource,
            "attributes": {"from": str(payload.get("from") or ""), "to": str(payload.get("to") or "")},
        })
        return out

    return out




