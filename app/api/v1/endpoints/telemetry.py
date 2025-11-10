from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, status, Depends
from pydantic import BaseModel, Field

from app.services.otel_exporter import get_export_status, set_export_enabled
from app.services.normalizers.dcim_http import get_redfish_status, set_redfish_enabled
from app.core.logging_config import get_request_logs_status, set_request_logs_enabled
from app.core.config import get_settings
from app.streams.automations import get_status as get_auto_status, set_dry_run as set_auto_dryrun
from app.rules.automations import get_rules as rules_get, upsert_rule as rules_upsert, delete_rule as rules_delete
import redis.asyncio as aioredis
from typing import Any
import json as _json
import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.api.deps import get_db_session
from app.models.data_source import DataSource


router = APIRouter()


class ExportToggle(BaseModel):
    enabled: bool


@router.get("/export/status")
async def export_status() -> dict[str, object]:
    return get_export_status()


@router.post("/export/status")
async def export_toggle(body: ExportToggle) -> dict[str, object]:
    return set_export_enabled(body.enabled)


@router.get("/redfish/status")
async def redfish_status() -> dict[str, object]:
    return get_redfish_status()


@router.post("/redfish/status")
async def redfish_toggle(body: ExportToggle) -> dict[str, object]:
    return set_redfish_enabled(body.enabled)


@router.get("/request-logs/status")
async def request_logs_status() -> dict[str, object]:
    return get_request_logs_status()


@router.post("/request-logs/status")
async def request_logs_toggle(body: ExportToggle) -> dict[str, object]:
    return set_request_logs_enabled(body.enabled)


@router.get("/metrics")
async def metrics_recent(limit: int = 100, vendor: str | None = None, schema: str | None = None) -> dict[str, Any]:
    """Return recent normalized metric points from the internal metrics stream.
    Optional filters: vendor (e.g., 'dcim_http', 'snmp') or schema ('redfish').
    """
    settings = get_settings()
    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    # fetch recent entries
    rows = await redis.xrevrange("metrics", count=max(1, min(limit, 1000)))
    items: list[dict[str, Any]] = []
    for _id, fields in rows:
        try:
            resource = _json.loads(fields.get("resource") or "{}")
        except Exception:
            resource = {}
        try:
            attributes = _json.loads(fields.get("attributes") or "{}")
        except Exception:
            attributes = {}
        obj = {
            "id": _id,
            "name": fields.get("name"),
            "type": fields.get("type"),
            "value": fields.get("value"),
            "unit": fields.get("unit"),
            "resource": resource,
            "attributes": attributes,
        }
        # filters
        if vendor and (resource.get("vendor") != vendor):
            continue
        if schema == "redfish" and not str(obj.get("name") or "").startswith("redfish."):
            continue
        items.append(obj)
    # reverse to chronological order
    items.reverse()
    return {"items": items}


class AutomationsToggle(BaseModel):
    dry_run: bool


@router.get("/automations/status")
async def automations_status() -> dict[str, object]:
    return get_auto_status()


@router.post("/automations/status")
async def automations_toggle(body: AutomationsToggle) -> dict[str, object]:
    set_auto_dryrun(body.dry_run)
    return get_auto_status()


class AutomationRule(BaseModel):
    id: str
    match: dict
    action: dict
    cooldown: str | None = None


@router.get("/automations/rules")
async def list_automation_rules() -> dict[str, object]:
    return {"rules": rules_get()}


@router.post("/automations/rules")
async def upsert_automation_rule(body: AutomationRule) -> dict[str, object]:
    rule = rules_upsert(body.model_dump())
    return {"rule": rule}


@router.delete("/automations/rules/{rule_id}")
async def delete_automation_rule(rule_id: str) -> dict[str, object]:
    ok = rules_delete(rule_id)
    return {"deleted": bool(ok)}


# --- Telegraf ingestion ---

class TelegrafMetric(BaseModel):
    name: str
    tags: dict[str, Any] = Field(default_factory=dict)
    fields: dict[str, Any] = Field(default_factory=dict)
    timestamp: int | None = None  # seconds epoch


class TelegrafBatch(BaseModel):
    metrics: list[TelegrafMetric] = Field(default_factory=list)


@router.post("/telegraf")
async def ingest_telegraf(
    batch: TelegrafBatch,
    x_telegraf_token: str | None = Header(default=None, convert_underscores=False),
    x_agent_id: str | None = Header(default=None, convert_underscores=False),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Accept Telegraf metrics and enqueue them to the central logs stream.

    - Log-like metrics (e.g., macos_log) are written as plain log lines for parsing/template routing.
    - Numeric metrics are written as JSON payloads with source kind 'telegraf' for normalization/export.
    """
    settings = get_settings()
    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

    # Authenticate agent via DataSource(type=telegraf, enabled=true) using token in config
    if not x_telegraf_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing telegraf token")

    # Load telegraf agents and find match by token (and optional agent_id)
    res = await db.execute(select(DataSource).where(DataSource.type == "telegraf", DataSource.enabled == True))
    agents = [row for row in res.scalars().all()]
    matched: DataSource | None = None
    for a in agents:
        cfg = a.config or {}
        tok = str(cfg.get("token") or "")
        if not tok or tok != x_telegraf_token:
            continue
        aid = str(cfg.get("agent_id") or "")
        if aid and x_agent_id and aid != x_agent_id:
            continue
        matched = a
        break
    if matched is None:
        # incremental metric for failed auth
        try:
            await redis.incr("telegraf:auth:rejected")
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid or disabled token")

    # Optional host allow-list enforcement
    allowed_hosts = (matched.config or {}).get("allowed_hosts") or []
    provided_hosts = {str((m.tags or {}).get("host") or "") for m in batch.metrics}
    if allowed_hosts:
        if not any(h and h in allowed_hosts for h in provided_hosts):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="host not allowed for this agent")

    logs_written = 0
    metrics_written = 0

    for m in batch.metrics:
        name = (m.name or "").strip().lower()
        # Treat *os*_log as raw log lines
        msg = None
        if isinstance(m.fields, dict):
            val = m.fields.get("message")
            if isinstance(val, str) and val:
                msg = val

        if name in {"macos_log", "linux_log", "windows_log"} and msg:
            # Map to existing OS file names so consumer routes correctly by substring
            source_map = {
                "macos_log": "Mac.log:telegraf",
                "linux_log": "Linux.log:telegraf",
                "windows_log": "Windows_2k.log:telegraf",
            }
            source = source_map.get(name, "Mac.log:telegraf")
            await redis.xadd("logs", {"source": source, "line": msg, "source_id": str(matched.id)})
            logs_written += 1
            continue

        # Otherwise, enqueue as telegraf JSON for normalization in consumer
        payload = {
            "name": m.name,
            "tags": m.tags or {},
            "fields": m.fields or {},
            "timestamp": m.timestamp,
        }
        # Use source prefix 'telegraf' so consumer can normalize
        host = str((m.tags or {}).get("host") or "")
        await redis.xadd("logs", {"source": f"telegraf:{host}", "line": json.dumps(payload), "source_id": str(matched.id)})
        metrics_written += 1

    # agent runtime stats
    try:
        key = f"telegraf:agent:{matched.id}"
        pipe = redis.pipeline()
        pipe.hset(key, mapping={
            "last_seen": str(int(__import__('time').time())),
            "name": matched.name,
        })
        pipe.incrby(f"{key}:accepted", logs_written + metrics_written)
        await pipe.execute()
    except Exception:
        pass

    return {"accepted": len(batch.metrics), "logs_enqueued": logs_written, "metrics_enqueued": metrics_written, "source_id": matched.id}


