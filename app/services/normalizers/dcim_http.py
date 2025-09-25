from __future__ import annotations

from typing import Any, Dict, List

from app.services.metrics_normalization import MetricPoint, register_normalizer, now_nano

# Runtime toggle & stats for Redfish normalization
_redfish_runtime_enabled: bool | None = None
_redfish_total_normalized: int = 0
_redfish_last_ns: int = 0


def get_redfish_status() -> dict[str, object]:
    from datetime import datetime, timezone
    last_iso = None
    if _redfish_last_ns:
        last_iso = datetime.fromtimestamp(_redfish_last_ns / 1e9, tz=timezone.utc).isoformat()
    return {
        "enabled": (_redfish_runtime_enabled if _redfish_runtime_enabled is not None else True),
        "total_normalized": _redfish_total_normalized,
        "last_time": last_iso,
    }


def set_redfish_enabled(value: bool) -> dict[str, object]:
    global _redfish_runtime_enabled
    _redfish_runtime_enabled = bool(value)
    return get_redfish_status()


def _iter_extractors(body: Any, cfg: Dict[str, Any]) -> List[MetricPoint]:
    res: List[MetricPoint] = []
    for ex in (cfg.get("extract") or []):
        node = body
        for k in (ex.get("path") or []):
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        arr = node if isinstance(node, list) else []
        for item in arr:
            field = ex.get("field", "")
            if not field or not isinstance(item, dict):
                continue
            val = item.get(field)
            if val is None:
                continue
            try:
                num = float(val)
            except Exception:
                continue
            mp: MetricPoint = {
                "name": ex.get("name", "dcim.metric"),
                "type": ex.get("type", "gauge"),
                "value": num,
                "unit": ex.get("unit"),
                "time_unix_nano": now_nano(),
                "resource": {"vendor": "dcim_http"},
                "attributes": {},
            }
            ak = ex.get("attr_key")
            if ak and ak in item:
                mp["attributes"][ak] = item[ak]
            res.append(mp)
    return res


@register_normalizer("dcim_http")
def normalize_dcim(_: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> List[MetricPoint]:
    # payload example from producer: {"url","status","body"}
    body = payload.get("body")
    if not isinstance(body, dict):
        return []
    if cfg.get("schema") == "redfish":
        enabled = _redfish_runtime_enabled if _redfish_runtime_enabled is not None else True
        if not enabled:
            return []
        # Default Redfish thermal temperatures mapping
        points = _iter_extractors(
            body,
            {
                "extract": [
                    {
                        "name": "redfish.temperature.celsius",
                        "unit": "C",
                        "path": ["Thermal", "Temperatures"],
                        "field": "ReadingCelsius",
                        "attr_key": "Name",
                    }
                ]
            },
        )
        if points:
            global _redfish_total_normalized, _redfish_last_ns
            _redfish_total_normalized += len(points)
            import time as _time
            _redfish_last_ns = int(_time.time() * 1e9)
        return points
    return _iter_extractors(body, cfg)


