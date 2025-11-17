import asyncio
import logging
from collections import defaultdict
from typing import Any, Dict, List

import redis.asyncio as aioredis
from redis.exceptions import ResponseError
from fastapi import FastAPI
import threading

from app.core.config import get_settings
from app.services.chroma_service import ChromaClientProvider
from app.services.failure_rules import match_failure_signals
from app.services.prototype_router import nearest_prototype
from app.parsers.linux import parse_linux_line
from app.parsers.macos import parse_macos_line
from app.parsers.templating import render_templated_line
from app.services.metrics_normalization import normalize
# Ensure normalizers are registered at import time
from app.services.normalizers import telegraf as _telegraf_norm  # noqa: F401
from app.services.normalizers import dcim_http as _dcim_http_norm  # noqa: F401
from app.services.normalizers import snmp as _snmp_norm  # noqa: F401
from app.services.normalizers import redfish as _redfish_norm  # noqa: F401
from app.services.otel_exporter import export_metrics
from app.db.session import AsyncSessionLocal
from app.models.data_source import DataSource

settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

STREAM_NAME = "logs"
GROUP_NAME = "log_consumers"
CONSUMER_NAME = "consumer_1"
METRICS_STREAM = "metrics"

_provider: ChromaClientProvider | None = None
LOG = logging.getLogger(__name__)


def _get_provider() -> ChromaClientProvider:
    global _provider
    if _provider is None:
        _provider = ChromaClientProvider()
    return _provider


def _os_from_source(source: str | None) -> str:
    if not source:
        return "unknown"
    s = source.lower()
    if "linux.log" in s:
        return "linux"
    if "mac.log" in s:
        return "macos"
    if "windows" in s:
        return "windows"
    return "unknown"


def _log_collection_name(os_name: str) -> str:
    return f"{settings.CHROMA_LOG_COLLECTION_PREFIX}{os_name or 'unknown'}"


def _parse_and_template(os_name: str, line: str) -> tuple[str, Dict[str, str]]:
    parsed: Dict[str, str] | None = None
    if os_name == "linux":
        parsed = parse_linux_line(0, line) or None
    elif os_name == "macos":
        parsed = parse_macos_line(0, line) or None
    if not parsed:
        templated = render_templated_line(component="unknown", pid=None, content=line)
        return templated, {"content": line, "component": "unknown"}
    templated = render_templated_line(
        component=parsed.get("component", ""),
        pid=parsed.get("PID"),
        content=parsed.get("content", ""),
    )
    return templated, parsed


async def consume_logs():
    """Consume new messages from Redis Stream and acknowledge them."""
    # create consumer group if not exists
    try:
        await redis.xgroup_create(STREAM_NAME, GROUP_NAME, id="$", mkstream=True)
        LOG.info("consumer group created stream=%s group=%s", STREAM_NAME, GROUP_NAME)
    except ResponseError as exc:
        LOG.info("consumer group exists stream=%s group=%s info=%s", STREAM_NAME, GROUP_NAME, exc)

    LOG.info("consumer ready and entering read loop stream=%s group=%s consumer=%s", STREAM_NAME, GROUP_NAME, CONSUMER_NAME)

    while True:
        try:
            response = await redis.xreadgroup(
                GROUP_NAME,
                CONSUMER_NAME,
                {STREAM_NAME: ">"},
                count=50,
                block=1000,
            )
        except Exception as exc:
            LOG.info("xreadgroup failed stream=%s group=%s consumer=%s err=%s", STREAM_NAME, GROUP_NAME, CONSUMER_NAME, exc)
            await asyncio.sleep(1)
            continue
        if not response:
            continue

        provider = _get_provider()
        # Accumulate per collection for batch upserts
        batched: dict[str, dict[str, List[Any]]] = defaultdict(lambda: {"ids": [], "documents": [], "metadatas": []})
        candidates: List[Dict[str, Any]] = []
        ack_ids: List[str] = []

        total_msgs = 0
        for _, messages in response:
            for msg_id, data in messages:
                try:
                    total_msgs += 1
                    source = data.get("source")
                    line = data.get("line") or ""
                    kind = (source or "").split(":", 1)[0]

                    # Normalize metrics for supported kinds and optionally export to OTel
                    if settings.ENABLE_METRICS_NORMALIZATION and kind in {"snmp", "dcim_http", "telegraf", "redfish", "scom", "squaredup", "catalyst", "thousandeyes", "bluecat"}:
                        payload_obj = None
                        try:
                            import json as _json
                            payload_obj = _json.loads(line)
                        except Exception:
                            payload_obj = None
                        if isinstance(payload_obj, dict):
                            # find DataSource config by source_id if present
                            cfg: Dict[str, Any] = {}
                            try:
                                src_id_str = data.get("source_id")
                                if src_id_str:
                                    src_id = int(src_id_str)
                                    async with AsyncSessionLocal() as db:  # type: ignore
                                        row = await db.get(DataSource, src_id)
                                    if row and isinstance(row.config, dict):
                                        cfg = row.config
                            except Exception:
                                cfg = {}
                            try:
                                points = normalize(kind, payload_obj, cfg or {})
                            except Exception as ne:
                                logging.getLogger("app.kaboom").info(
                                    "normalize_failed kind=%s src_id=%s err=%s line_len=%s",
                                    kind, data.get("source_id"), ne, len(line or ""),
                                )
                                points = []
                            if points:
                                try:
                                    LOG.info("consumer: normalized metrics kind=%s points=%d", kind, len(points))
                                except Exception as e_xadd:
                                    logging.getLogger("app.kaboom").info(
                                        "metrics_xadd_failed kind=%s err=%s name=%s", kind, e_xadd, mp.get("name")
                                    )
                                # Export to OTEL if enabled
                                export_metrics(points)
                                # Also write to Redis metrics stream for internal uses
                                for mp in points:
                                    try:
                                        import json as _json
                                        await redis.xadd(METRICS_STREAM, {
                                            "name": mp.get("name", ""),
                                            "type": mp.get("type", "gauge"),
                                            "value": str(mp.get("value", "")),
                                            "unit": (mp.get("unit") or ""),
                                            "resource": _json.dumps(mp.get("resource") or {}),
                                            "attributes": _json.dumps(mp.get("attributes") or {}),
                                        })
                                    except Exception:
                                        pass
                                # Derive incident candidates from normalized telemetry (network-aware)
                                try:
                                    norm_candidates: List[Dict[str, Any]] = []
                                    # ThousandEyes candidates
                                    if kind == "thousandeyes":
                                        typ = str(payload_obj.get("type") or "")
                                        if typ == "alert":
                                            sev = str(payload_obj.get("severity") or payload_obj.get("level") or "").lower()
                                            if sev in {"warning", "major", "critical"}:
                                                norm_candidates.append({
                                                    "os": "network",
                                                    "raw": line,
                                                    "templated": f"thousandeyes alert {sev} {payload_obj.get('ruleName','')}",
                                                    "rule_label": "network_alert",
                                                    "rule_score": 1.0,
                                                    "nearest_distance": "",
                                                    "nearest_label": "",
                                                })
                                        elif typ == "test":
                                            metrics = payload_obj.get("metrics") or {}
                                            # Support both 'latencyMs' and 'avgLatency' forms
                                            lat = metrics.get("latencyMs", payload_obj.get("avgLatency"))
                                            loss = metrics.get("loss")
                                            if (isinstance(lat, (int, float)) and lat > 150) or (isinstance(loss, (int, float)) and loss > 1.0):
                                                norm_candidates.append({
                                                    "os": "network",
                                                    "raw": line,
                                                    "templated": f"thousandeyes test latency={lat}ms loss={loss}%",
                                                    "rule_label": "network_performance",
                                                    "rule_score": 0.9,
                                                    "nearest_distance": "",
                                                    "nearest_label": "",
                                                })
                                    # Catalyst candidates
                                    if kind == "catalyst":
                                        typ = str(payload_obj.get("type") or "")
                                        if typ == "event":
                                            sev = str(payload_obj.get("severity") or "").lower()
                                            if sev in {"major", "critical"}:
                                                norm_candidates.append({
                                                    "os": "network",
                                                    "raw": line,
                                                    "templated": f"catalyst event {sev} {payload_obj.get('name','')}",
                                                    "rule_label": "network_event",
                                                    "rule_score": 1.0,
                                                    "nearest_distance": "",
                                                    "nearest_label": "",
                                                })
                                    # SCOM candidates
                                    if kind == "scom":
                                        typ = str(payload_obj.get("type") or "")
                                        if typ == "alert":
                                            sev = str(payload_obj.get("Severity") or payload_obj.get("severity") or "").lower()
                                            name = str(payload_obj.get("Name") or payload_obj.get("name") or "")
                                            src = str(payload_obj.get("MonitoringObjectDisplayName") or "")
                                            if sev in {"warning", "error", "critical"} or name:
                                                norm_candidates.append({
                                                    "os": "windows",
                                                    "raw": line,
                                                    "templated": f"scom alert {sev} {name} source={src}".strip(),
                                                    "rule_label": "windows_alert",
                                                    "rule_score": 1.0,
                                                    "nearest_distance": "",
                                                    "nearest_label": "",
                                                })
                                        elif typ in {"performance", "event"}:
                                            # Defer to generic fallback below
                                            pass
                                    # SquaredUp candidates
                                    if kind == "squaredup":
                                        typ = str(payload_obj.get("type") or "")
                                        if typ == "alert":
                                            sev = str(payload_obj.get("severity") or "").lower()
                                            title = str(payload_obj.get("title") or "")
                                            if sev in {"warning", "error", "critical"} or title:
                                                norm_candidates.append({
                                                    "os": "windows",
                                                    "raw": line,
                                                    "templated": f"squaredup alert {sev} {title}".strip(),
                                                    "rule_label": "windows_alert",
                                                    "rule_score": 1.0,
                                                    "nearest_distance": "",
                                                    "nearest_label": "",
                                                })
                                        elif typ == "health":
                                            state = str(payload_obj.get("state") or payload_obj.get("status") or "").lower()
                                            name = str(payload_obj.get("name") or "")
                                            if state and state not in {"ok", "healthy", "green"}:
                                                norm_candidates.append({
                                                    "os": "windows",
                                                    "raw": line,
                                                    "templated": f"squaredup health {state} {name}".strip(),
                                                    "rule_label": "windows_health",
                                                    "rule_score": 0.9,
                                                    "nearest_distance": "",
                                                    "nearest_label": "",
                                                })
                                except Exception as e_pub:
                                    logging.getLogger("app.kaboom").info(
                                        "norm_candidate_publish_failed kind=%s err=%s summary_len=%s",
                                        kind, e_pub, len((c.get("templated") or c.get("raw") or "")),
                                    )
                                # Publish any generated candidates immediately to issues stream
                                try:
                                    import json as _json
                                    for c in norm_candidates:
                                        # Publish in incidents-friendly shape so UI can render immediately
                                        logs_field = _json.dumps([{
                                            "templated": c.get("templated", ""),
                                            "raw": c.get("raw", ""),
                                        }])
                                        _eid = await redis.xadd(settings.ISSUES_CANDIDATES_STREAM, {
                                            "os": c.get("os", "unknown"),
                                            "issue_key": c.get("issue_key", ""),
                                            "templated_summary": c.get("templated", "") or c.get("raw", ""),
                                            "logs": logs_field,
                                        })
                                        try:
                                            logging.getLogger("app.kaboom").info(
                                                "norm_incident_published id=%s kind=%s os=%s", _eid, kind, c.get("os")
                                            )
                                        except Exception:
                                            pass
                                    # Fallback: if no specific candidates matched, publish a generic incident
                                    if not norm_candidates and kind in {"thousandeyes", "catalyst", "snmp", "dcim_http", "telegraf", "bluecat", "scom", "squaredup"}:
                                        summary_text = ""
                                        try:
                                            # Prefer concise summary from payload if available
                                            summary_text = str(payload_obj.get("summary") or payload_obj.get("name") or payload_obj.get("type") or "")[:200]
                                        except Exception:
                                            summary_text = ""
                                        if not summary_text:
                                            # Fall back to the raw line (truncated)
                                            summary_text = (line[:200] if isinstance(line, str) else "")
                                        # Choose os by kind for fallback
                                        fallback_os = "network"
                                        if kind in {"scom", "squaredup"}:
                                            fallback_os = "windows"
                                        _eid2 = await redis.xadd(settings.ISSUES_CANDIDATES_STREAM, {
                                            "os": fallback_os,
                                            "issue_key": "",
                                            "templated_summary": summary_text,
                                            "logs": _json.dumps([{
                                                "templated": summary_text,
                                                "raw": line if isinstance(line, str) else "",
                                            }]),
                                        })
                                        try:
                                            logging.getLogger("app.kaboom").info(
                                                "norm_incident_published id=%s kind=%s os=%s", _eid2, kind, "network"
                                            )
                                        except Exception:
                                            pass
                                except Exception as e_pub2:
                                    logging.getLogger("app.kaboom").info(
                                        "norm_generic_incident_publish_failed kind=%s err=%s", kind, e_pub2
                                    )
                    # If metrics normalization is disabled, still derive basic incidents for SCOM/SquaredUp
                    if not settings.ENABLE_METRICS_NORMALIZATION and kind in {"scom", "squaredup"}:
                        try:
                            import json as _json
                            payload_obj2 = None
                            try:
                                payload_obj2 = _json.loads(line)
                            except Exception:
                                payload_obj2 = None
                            if isinstance(payload_obj2, dict):
                                norm_candidates2: List[Dict[str, Any]] = []
                                if kind == "scom":
                                    typ = str(payload_obj2.get("type") or "")
                                    if typ == "alert":
                                        sev = str(payload_obj2.get("Severity") or payload_obj2.get("severity") or "").lower()
                                        name = str(payload_obj2.get("Name") or payload_obj2.get("name") or "")
                                        src = str(payload_obj2.get("MonitoringObjectDisplayName") or "")
                                        if sev in {"warning", "error", "critical"} or name:
                                            norm_candidates2.append({
                                                "os": "windows",
                                                "raw": line,
                                                "templated": f"scom alert {sev} {name} source={src}".strip(),
                                            })
                                if kind == "squaredup":
                                    typ = str(payload_obj2.get("type") or "")
                                    if typ == "alert":
                                        sev = str(payload_obj2.get("severity") or "").lower()
                                        title = str(payload_obj2.get("title") or "")
                                        if sev in {"warning", "error", "critical"} or title:
                                            norm_candidates2.append({
                                                "os": "windows",
                                                "raw": line,
                                                "templated": f"squaredup alert {sev} {title}".strip(),
                                            })
                                    elif typ == "health":
                                        state = str(payload_obj2.get("state") or payload_obj2.get("status") or "").lower()
                                        name0 = str(payload_obj2.get("name") or "")
                                        if state and state not in {"ok", "healthy", "green"}:
                                            norm_candidates2.append({
                                                "os": "windows",
                                                "raw": line,
                                                "templated": f"squaredup health {state} {name0}".strip(),
                                            })
                                # Generic fallback for these kinds
                                if not norm_candidates2:
                                    summary_text2 = str(payload_obj2.get("summary") or payload_obj2.get("name") or payload_obj2.get("type") or "")[:200]
                                    if not summary_text2:
                                        summary_text2 = (line[:200] if isinstance(line, str) else "")
                                    norm_candidates2.append({
                                        "os": "windows",
                                        "raw": line,
                                        "templated": summary_text2,
                                    })
                                # Publish
                                for c2 in norm_candidates2:
                                    logs_field2 = _json.dumps([{
                                        "templated": c2.get("templated", ""),
                                        "raw": c2.get("raw", ""),
                                    }])
                                    _eidx = await redis.xadd(settings.ISSUES_CANDIDATES_STREAM, {
                                        "os": c2.get("os", "windows"),
                                        "issue_key": "",
                                        "templated_summary": c2.get("templated", "") or c2.get("raw", ""),
                                        "logs": logs_field2,
                                    })
                                    try:
                                        logging.getLogger("app.kaboom").info(
                                            "norm_incident_published id=%s kind=%s os=%s", _eidx, kind, c2.get("os")
                                        )
                                    except Exception:
                                        pass
                        except Exception:
                            pass

                    # Infer domain/OS
                    os_name = _os_from_source(source)
                    if os_name == "unknown":
                        # Map known producer kinds to domains
                        if kind in {"scom", "squaredup"}:
                            os_name = "windows"
                        elif kind in {"thousandeyes", "catalyst", "snmp", "dcim_http"}:
                            os_name = "network"
                        elif kind in {"redfish"}:
                            os_name = "linux"
                    templated, parsed = _parse_and_template(os_name, line)

                    # Choose document text based on embedding mode
                    use_raw = settings.EMBEDDING_PROVIDER.lower() == "logbert" and getattr(settings, "LOGBERT_USE_RAW_LOGS", False)
                    doc_text = line if use_raw else templated

                    # route to logs_<os>
                    coll_name = _log_collection_name(os_name)
                    batched[coll_name]["ids"].append(msg_id)
                    batched[coll_name]["documents"].append(doc_text)
                    batched[coll_name]["metadatas"].append({
                        "os": os_name,
                        "source": source or "",
                        "raw": line,
                        "embedding_mode": "raw" if use_raw else "templated",
                        **parsed,
                    })

                    # quick rule signal
                    rule = match_failure_signals(f"{templated} {line}")

                    # nearest prototype distance (guard failures)
                    distance = None
                    label = None
                    try:
                        query_text = doc_text  # align with what's stored and embedded
                        nearest = nearest_prototype(os_name, query_text, k=1)
                        distance = nearest[0]["distance"] if nearest else None
                        label = (nearest[0]["metadata"] or {}).get("label") if nearest else None
                    except Exception as exc:
                        LOG.info("prototype routing failed os=%s err=%s", os_name, exc)

                    should_candidate = False
                    if rule.get("has_signal"):
                        should_candidate = True
                    if distance is None or (isinstance(distance, (int, float)) and distance > settings.NEAREST_PROTO_THRESHOLD):
                        should_candidate = True

                    if should_candidate:
                        candidates.append({
                            "os": os_name,
                            "raw": line,
                            "templated": templated,
                            "rule_label": rule.get("label"),
                            "rule_score": rule.get("score"),
                            "nearest_distance": distance if distance is not None else "",
                            "nearest_label": label or "",
                        })
                except Exception as exc:
                    LOG.info("consumer message processing failed id=%s err=%s", msg_id, exc)
                    try:
                        logging.getLogger("app.kaboom").info(
                            "consumer_failed id=%s source=%s kind=%s os_guess=%s err=%s line_len=%s",
                            msg_id, data.get("source"), (data.get("source") or "").split(":",1)[0],
                            _os_from_source(data.get("source")), exc, len((data.get("line") or "")),
                        )
                    except Exception:
                        pass
                finally:
                    ack_ids.append(msg_id)

        LOG.info("processing batch size=%d collections=%d candidates=%d", total_msgs, len(batched), len(candidates))
        # Perform upserts per collection
        for coll_name, payload in batched.items():
            try:
                collection = provider.get_or_create_collection(coll_name)
                if payload["ids"]:
                    collection.upsert(ids=payload["ids"], documents=payload["documents"], metadatas=payload["metadatas"])
                    LOG.info("upserted collection=%s count=%d", coll_name, len(payload["ids"]))
            except Exception:
                LOG.exception("upsert failed collection=%s", coll_name)

        # Publish per-line candidates if enabled
        if settings.ENABLE_PER_LINE_CANDIDATES:
            for c in candidates:
                try:
                    await redis.xadd(settings.ISSUES_CANDIDATES_STREAM, c)
                except Exception as exc:
                    LOG.info("publish candidate failed stream=%s err=%s", settings.ALERTS_CANDIDATES_STREAM, exc)

        # Acknowledge after successful writes
        if ack_ids:
            try:
                await redis.xack(STREAM_NAME, GROUP_NAME, *ack_ids)
                LOG.info("acked messages count=%d", len(ack_ids))
            except Exception as exc:
                LOG.info("ack failed count=%d err=%s", len(ack_ids), exc)


def attach_consumer(app: FastAPI):
    async def _run_forever():
        backoff = 1.0
        while True:
            try:
                LOG.info("starting consumer stream=%s group=%s consumer=%s", STREAM_NAME, GROUP_NAME, CONSUMER_NAME)
                await consume_logs()
            except Exception as exc:
                LOG.info("consumer crashed err=%s; restarting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    @app.on_event("startup")
    async def startup_event():
        LOG.info("starting consumer in dedicated thread")
        loop = asyncio.new_event_loop()

        def _runner():
            asyncio.set_event_loop(loop)
            loop.create_task(_run_forever())
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="consumer-thread", daemon=True)
        thread.start()
        app.state.consumer_loop = loop
        app.state.consumer_thread = thread

    @app.on_event("shutdown")
    async def shutdown_event():
        LOG.info("stopping consumer thread")
        loop = getattr(app.state, "consumer_loop", None)
        thread = getattr(app.state, "consumer_thread", None)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
