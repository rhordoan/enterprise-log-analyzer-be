from __future__ import annotations

from typing import Any, Dict, List

from app.services.metrics_normalization import MetricPoint, register_normalizer, now_nano


@register_normalizer("scom")
def normalize_scom(_: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> List[MetricPoint]:
    # Payload is JSON from SCOM producer: {"type":"alert|performance|event", ...}
    typ = str(payload.get("type") or "")
    t_nano = now_nano()
    resource = {"vendor": "scom"}
    if "ComputerName" in payload:
        resource["host"] = str(payload.get("ComputerName") or "")
    out: List[MetricPoint] = []

    if typ == "performance":
        # Try mapping to scom.perf.<counter>
        # Common fields: ObjectName, CounterName, InstanceName, Value
        obj = str(payload.get("ObjectName") or payload.get("object") or "").lower()
        counter = str(payload.get("CounterName") or payload.get("counter") or "").lower()
        inst = str(payload.get("InstanceName") or payload.get("instance") or "")
        val = payload.get("Value") if "Value" in payload else payload.get("value")
        try:
            num = float(val)
        except Exception:
            return []
        name_parts = ["scom", "perf"]
        if obj:
            name_parts.append(obj.replace(" ", "_"))
        if counter:
            name_parts.append(counter.replace(" ", "_"))
        metric_name = ".".join(name_parts)
        attrs: Dict[str, Any] = {}
        if inst:
            attrs["instance"] = inst
        out.append({
            "name": metric_name,
            "type": "gauge",
            "value": num,
            "unit": None,
            "time_unix_nano": t_nano,
            "resource": resource,
            "attributes": attrs,
        })
        return out

    if typ == "alert":
        sev = str(payload.get("Severity") or payload.get("severity") or "").lower()
        pri = str(payload.get("Priority") or payload.get("priority") or "").lower()
        # map severity to 0/1/2
        sev_map = {"information": 0, "warning": 1, "error": 2, "critical": 2}
        sev_val = sev_map.get(sev, 0)
        out.append({
            "name": "scom.alert.severity",
            "type": "gauge",
            "value": sev_val,
            "unit": None,
            "time_unix_nano": t_nano,
            "resource": resource,
            "attributes": {
                "priority": pri or "",
                "id": str(payload.get("Id") or payload.get("id") or ""),
                "name": str(payload.get("Name") or payload.get("name") or ""),
                "source": str(payload.get("MonitoringObjectDisplayName") or ""),
            },
        })
        return out

    if typ == "event":
        # Count events by level if present
        level = str(payload.get("LevelDisplayName") or payload.get("level") or "").lower()
        out.append({
            "name": "scom.event.count",
            "type": "sum",
            "value": 1,
            "unit": None,
            "time_unix_nano": t_nano,
            "resource": resource,
            "attributes": {"level": level},
        })
        return out

    return out





