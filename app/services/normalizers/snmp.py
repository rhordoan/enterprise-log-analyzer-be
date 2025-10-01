from __future__ import annotations

from typing import Any, Dict, List

from app.services.metrics_normalization import MetricPoint, register_normalizer, now_nano


@register_normalizer("snmp")
def normalize_snmp(_: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> List[MetricPoint]:
    # payload example from producer: {"host","port","community":"***","oid","value"}
    # cfg example: {"mappings":[{"oid":"1.3.6.1.2.1.1.3.0","name":"system.uptime","unit":"s","type":"gauge","scale":0.01}]}
    oid = str(payload.get("oid") or "")
    val = payload.get("value")
    host = str(payload.get("host") or "")
    mappings = {m["oid"]: m for m in (cfg.get("mappings") or []) if isinstance(m, dict) and m.get("oid")}
    m = mappings.get(oid)
    if not m:
        return []
    try:
        num = float(val)
        if "scale" in m:
            num *= float(m["scale"])
    except Exception:
        return []
    mp: MetricPoint = {
        "name": str(m.get("name") or oid),
        "type": str(m.get("type") or "gauge"),
        "value": num,
        "unit": m.get("unit"),
        "time_unix_nano": now_nano(),
        "resource": {"host": host, "vendor": "snmp"},
        "attributes": {"oid": oid},
    }
    return [mp]







