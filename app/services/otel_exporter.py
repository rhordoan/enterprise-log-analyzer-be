from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.core.config import get_settings


LOG = logging.getLogger(__name__)
settings = get_settings()


_otel_ready = False
_meter = None
_runtime_enabled: bool | None = None
_export_total: int = 0
_last_export_ns: int = 0


def _setup_otel() -> None:
    global _otel_ready, _meter
    try:
        from opentelemetry import metrics
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
        resource = Resource.create({"service.name": settings.OTEL_SERVICE_NAME})
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(provider)
        _meter = metrics.get_meter(settings.OTEL_SERVICE_NAME)
        _otel_ready = True
        LOG.info("OTel metrics exporter configured endpoint=%s", endpoint)
    except Exception as exc:  # noqa: BLE001
        LOG.info("OTel exporter setup failed err=%s (export disabled)", exc)
        _otel_ready = False


def export_metrics(points: List[Dict[str, Any]]) -> None:
    """Best-effort export: record metrics as histograms with attributes.
    Uses dynamic instruments by metric name. If OTel is not available, no-op.
    """
    global _export_total, _last_export_ns
    enabled = _runtime_enabled if _runtime_enabled is not None else settings.ENABLE_OTEL_EXPORT
    if not enabled:
        return
    if not _otel_ready:
        _setup_otel()
    if not _otel_ready or _meter is None:
        return
    try:
        # Avoid unbounded instrument creation by limiting unique names
        for mp in points[:1000]:
            name = str(mp.get("name") or "metric.value")
            unit = mp.get("unit")
            value = mp.get("value")
            attributes = {**(mp.get("resource") or {}), **(mp.get("attributes") or {})}
            # Use histogram to record numeric values generically
            hist = _meter.create_histogram(name, unit=unit or None)
            try:
                v = float(value)
            except Exception:
                continue
            hist.record(v, attributes=attributes)
            _export_total += 1
        # update last export time when at least one point was exported
        if points:
            import time as _time
            _last_export_ns = int(_time.time() * 1e9)
    except Exception as exc:  # noqa: BLE001
        LOG.info("OTel export failed err=%s", exc)


def get_export_status() -> dict[str, object]:
    from datetime import datetime, timezone
    last_iso = None
    if _last_export_ns:
        last_iso = datetime.fromtimestamp(_last_export_ns / 1e9, tz=timezone.utc).isoformat()
    return {
        "enabled": (_runtime_enabled if _runtime_enabled is not None else settings.ENABLE_OTEL_EXPORT),
        "total_exported": _export_total,
        "last_export_time": last_iso,
        "endpoint": settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        "service_name": settings.OTEL_SERVICE_NAME,
    }


def set_export_enabled(value: bool) -> dict[str, object]:
    global _runtime_enabled
    _runtime_enabled = bool(value)
    return get_export_status()


