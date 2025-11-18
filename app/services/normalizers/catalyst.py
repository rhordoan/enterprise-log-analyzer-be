from __future__ import annotations

from typing import Any, Dict, List

from app.services.metrics_normalization import MetricPoint, register_normalizer, now_nano


@register_normalizer("catalyst")
def normalize_catalyst(_: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> List[MetricPoint]:
    t = str(payload.get("type") or "")
    ts = now_nano()
    resource = {"vendor": "cisco_catalyst"}
    out: List[MetricPoint] = []

    if t.startswith("health_"):
        domain = t.split("_", 1)[1]
        # Health responses may include a healthScore or similar 0-100
        score = payload.get("healthScore") or payload.get("score") or payload.get("networkHealthAverage")
        try:
            val = float(score)
        except Exception:
            return out
        out.append({
            "name": f"cisco.cc.health.{domain}",
            "type": "gauge",
            "value": val,
            "unit": "%",
            "time_unix_nano": ts,
            "resource": resource,
            "attributes": {},
        })
        return out

    if t == "event":
        sev = str(payload.get("severity") or payload.get("category") or "").lower()
        out.append({
            "name": "cisco.cc.event.count",
            "type": "sum",
            "value": 1,
            "unit": None,
            "time_unix_nano": ts,
            "resource": resource,
            "attributes": {"severity": sev, "name": str(payload.get("name") or "")},
        })
        return out

    return out






