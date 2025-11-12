from __future__ import annotations

from typing import Any, Dict, List

from app.services.metrics_normalization import MetricPoint, register_normalizer, now_nano


def _num(val: Any) -> float | None:
    try:
        return float(val)
    except Exception:
        return None


@register_normalizer("redfish")
def normalize_redfish(_: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> List[MetricPoint]:
    host = str(payload.get("host") or "")
    kind = str(payload.get("kind") or "")
    body = payload.get("body") or {}
    out: List[MetricPoint] = []

    if kind == "thermal" and isinstance(body, dict):
        # Temperatures
        temps = body.get("Temperatures") or []
        if isinstance(temps, list):
            for t in temps:
                if not isinstance(t, dict):
                    continue
                val = _num(t.get("ReadingCelsius"))
                if val is None:
                    continue
                mp: MetricPoint = {
                    "name": "redfish.temperature.celsius",
                    "type": "gauge",
                    "value": val,
                    "unit": "C",
                    "time_unix_nano": now_nano(),
                    "resource": {"host": host, "vendor": "redfish"},
                    "attributes": {"name": t.get("Name"), "member_id": t.get("MemberId")},
                }
                out.append(mp)
        # Fans
        fans = body.get("Fans") or []
        if isinstance(fans, list):
            for f in fans:
                if not isinstance(f, dict):
                    continue
                val = _num(f.get("Reading"))
                if val is None:
                    continue
                unit = str(f.get("ReadingUnits") or "RPM")
                mp: MetricPoint = {
                    "name": "redfish.fan.speed",
                    "type": "gauge",
                    "value": val,
                    "unit": unit,
                    "time_unix_nano": now_nano(),
                    "resource": {"host": host, "vendor": "redfish"},
                    "attributes": {"name": f.get("Name"), "member_id": f.get("MemberId")},
                }
                out.append(mp)

    if kind == "power" and isinstance(body, dict):
        # Power consumed watts
        pc = body.get("PowerControl") or []
        if isinstance(pc, list):
            for p in pc:
                if not isinstance(p, dict):
                    continue
                val = _num(p.get("PowerConsumedWatts"))
                if val is not None:
                    mp: MetricPoint = {
                        "name": "redfish.power.consumed_watts",
                        "type": "gauge",
                        "value": val,
                        "unit": "W",
                        "time_unix_nano": now_nano(),
                        "resource": {"host": host, "vendor": "redfish"},
                        "attributes": {},
                    }
                    out.append(mp)
        # Voltages
        volts = body.get("Voltages") or []
        if isinstance(volts, list):
            for v in volts:
                if not isinstance(v, dict):
                    continue
                val = _num(v.get("ReadingVolts"))
                if val is None:
                    continue
                mp: MetricPoint = {
                    "name": "redfish.voltage.volts",
                    "type": "gauge",
                    "value": val,
                    "unit": "V",
                    "time_unix_nano": now_nano(),
                    "resource": {"host": host, "vendor": "redfish"},
                    "attributes": {"name": v.get("Name"), "member_id": v.get("MemberId")},
                }
                out.append(mp)

    return out













