from __future__ import annotations

from typing import Any, Dict, List

from app.services.metrics_normalization import MetricPoint, register_normalizer, now_nano


@register_normalizer("thousandeyes")
def normalize_thousandeyes(_: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> List[MetricPoint]:
    typ = str(payload.get("type") or "")
    ts = now_nano()
    resource = {"vendor": "thousandeyes"}
    out: List[MetricPoint] = []

    if typ == "alert":
        sev = str(payload.get("severity") or payload.get("level") or "").lower()
        sev_map = {"info": 0, "informational": 0, "minor": 1, "warning": 1, "major": 2, "critical": 3}
        out.append({
            "name": "thousandeyes.alert.severity",
            "type": "gauge",
            "value": float(sev_map.get(sev, 0)),
            "unit": None,
            "time_unix_nano": ts,
            "resource": resource,
            "attributes": {"testId": str(payload.get("testId") or ""), "rule": str(payload.get("ruleName") or "")},
        })
        return out

    if typ == "test":
        # Common metrics: avgLatency (ms), loss (percent)
        lat = payload.get("avgLatency")
        loss = payload.get("loss")
        if isinstance(lat, (int, float)):
            out.append({
                "name": "thousandeyes.test.latency_ms",
                "type": "gauge",
                "value": float(lat),
                "unit": "ms",
                "time_unix_nano": ts,
                "resource": resource,
                "attributes": {"testId": str(payload.get("testId") or "")},
            })
        if isinstance(loss, (int, float)):
            out.append({
                "name": "thousandeyes.test.loss_pct",
                "type": "gauge",
                "value": float(loss),
                "unit": "%",
                "time_unix_nano": ts,
                "resource": resource,
                "attributes": {"testId": str(payload.get("testId") or "")},
            })
        return out

    return out





