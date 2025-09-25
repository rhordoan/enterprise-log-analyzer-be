from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.otel_exporter import get_export_status, set_export_enabled
from app.services.normalizers.dcim_http import get_redfish_status, set_redfish_enabled
from app.core.config import get_settings
from app.streams.automations import get_status as get_auto_status, set_dry_run as set_auto_dryrun
from app.rules.automations import get_rules as rules_get, upsert_rule as rules_upsert, delete_rule as rules_delete
import redis.asyncio as aioredis
from typing import Any
import json as _json


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


