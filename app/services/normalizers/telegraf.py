from __future__ import annotations

from typing import Any, Dict, List

from app.services.metrics_normalization import MetricPoint, register_normalizer, now_nano


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


@register_normalizer("telegraf")
def normalize_telegraf(_: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> List[MetricPoint]:
    name = str(payload.get("name") or "")
    tags = payload.get("tags") or {}
    fields = payload.get("fields") or {}
    ts = payload.get("timestamp")
    host = str(tags.get("host") or "")
    device = str(tags.get("device") or "")
    path = str(tags.get("path") or "")

    out: List[MetricPoint] = []
    t_nano = now_nano() if not isinstance(ts, (int, float)) else int(float(ts) * 1e9)

    def mp(name: str, value: Any, unit: str | None = None, attributes: Dict[str, Any] | None = None) -> None:
        val_num = _to_float(value)
        if val_num is None:
            return
        pt: MetricPoint = {
            "name": name,
            "type": "gauge",
            "value": val_num,
            "unit": unit,
            "time_unix_nano": t_nano,
            "resource": {"host": host, "vendor": "telegraf"},
            "attributes": attributes or {},
        }
        out.append(pt)

    lname = name.lower()
    if lname == "cpu_temperature":
        mp("system.cpu.temperature", fields.get("value"), "C")
        return out

    if lname == "smart_device":
        # health_ok as 1/0
        if "health_ok" in fields:
            mp("smart.health_ok", 1 if bool(fields.get("health_ok")) else 0, None, {"device": device})
        if "power_on_hours" in fields:
            mp("smart.power_on_hours", fields.get("power_on_hours"), "h", {"device": device})
        return out

    if lname == "disk":
        if "used_percent" in fields:
            mp("fs.used_percent", fields.get("used_percent"), "%", {"path": path})
        return out

    # Generic single-value mapping
    if "value" in fields:
        mp(f"telegraf.{lname}", fields.get("value"))
    return out



