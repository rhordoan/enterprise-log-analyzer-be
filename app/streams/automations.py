from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Any, Dict

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI

from app.core.config import get_settings
from app.rules.automations import load_rules


LOG = logging.getLogger(__name__)
settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

_runtime_enabled: bool | None = None
_dry_run: bool = True
_total_triggered: int = 0
_provider_counts: Dict[str, int] = {}
_last_trigger_iso: str | None = None


def set_enabled(value: bool) -> None:
    global _runtime_enabled
    _runtime_enabled = bool(value)


def set_dry_run(value: bool) -> None:
    global _dry_run
    _dry_run = bool(value)


def get_status() -> Dict[str, Any]:
    return {
        "enabled": (_runtime_enabled if _runtime_enabled is not None else False),
        "dry_run": _dry_run,
        "total_triggered": _total_triggered,
        "provider_counts": dict(_provider_counts),
        "last_trigger_time": _last_trigger_iso,
    }


def _render(template: str, alert: Dict[str, Any]) -> str:
    s = template
    for k, v in alert.items():
        if isinstance(v, (dict, list)):
            continue
        s = s.replace(f"{{{{ alert.{k} }}}}", str(v))
    # nested common fields
    res = alert.get("result") or {}
    if isinstance(res, dict):
        for k, v in res.items():
            if isinstance(v, (dict, list)):
                continue
            s = s.replace(f"{{{{ alert.result.{k} }}}}", str(v))
    return s


async def _cooldown_guard(rule_id: str, key: str, cooldown: str) -> bool:
    # cooldown like "15m" / "1h" / seconds (default)
    try:
        unit = cooldown[-1].lower()
        num = int(cooldown[:-1])
        sec = num * (60 if unit == "m" else 3600 if unit == "h" else 1)
    except Exception:
        sec = 900
    redis_key = f"auto:cooldown:{rule_id}:{key}"
    ok = await redis.set(redis_key, "1", ex=sec, nx=True)
    return bool(ok)


def _match(rule: Dict[str, Any], alert: Dict[str, Any]) -> bool:
    m = rule.get("match") or {}
    failure_type = alert.get("failure_type") or (alert.get("result") or {}).get("failure_type")
    issue_key = alert.get("issue_key")
    try:
        confidence = float(alert.get("confidence") or (alert.get("result") or {}).get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    if m.get("failure_type") and m["failure_type"] != str(failure_type):
        return False
    if m.get("issue_key") and m["issue_key"] != str(issue_key):
        return False
    min_conf = float(m.get("min_confidence") or 0.0)
    return confidence >= min_conf


async def _ansible_tower(params: Dict[str, Any], alert: Dict[str, Any]) -> None:
    base = (params.get("base_url") or "").rstrip("/")
    jt = params.get("job_template_id")
    if not base or not jt:
        return
    url = f"{base}/api/v2/job_templates/{jt}/launch/"
    token = os.environ.get("TOWER_TOKEN") or params.get("token") or ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    extra_vars = {k: _render(str(v), alert) for k, v in (params.get("extra_vars") or {}).items()}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json={"extra_vars": extra_vars})
        r.raise_for_status()


async def _terraform_cloud(params: Dict[str, Any], alert: Dict[str, Any]) -> None:
    token = os.environ.get("TFC_TOKEN") or params.get("token") or ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    ws = params.get("workspace_id")
    msg = _render(params.get("message", "Automated run"), alert)
    if not ws:
        return
    async with httpx.AsyncClient(base_url="https://app.terraform.io/api/v2", timeout=30, headers=headers) as client:
        payload = {
            "data": {
                "attributes": {"message": msg, "plan-only": False},
                "type": "runs",
                "relationships": {"workspace": {"data": {"type": "workspaces", "id": ws}}},
            }
        }
        r = await client.post("/runs", json=payload)
        r.raise_for_status()


async def _servicenow(params: Dict[str, Any], alert: Dict[str, Any]) -> None:
    base = (params.get("base_url") or "").rstrip("/")
    if not base:
        return
    table = params.get("table") or "incident"
    user = os.environ.get("SN_USER") or params.get("user") or ""
    password = os.environ.get("SN_PASSWORD") or params.get("password") or ""
    payload = {k: _render(str(v), alert) for k, v in (params.get("payload") or {}).items()}
    auth = (user, password) if user or password else None
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{base}/api/now/table/{table}", auth=auth, json=payload)
        r.raise_for_status()


PROVIDERS = {
    "ansible_tower": _ansible_tower,
    "terraform_cloud": _terraform_cloud,
    "servicenow": _servicenow,
}


async def run_automations() -> None:
    global _total_triggered, _provider_counts, _last_trigger_iso
    rules = load_rules()
    group = "automations"
    consumer = "auto_1"
    try:
        await redis.xgroup_create(settings.ALERTS_STREAM, group, id="$", mkstream=True)
    except Exception:
        pass
    while True:
        try:
            enabled = (_runtime_enabled if _runtime_enabled is not None else settings.ENABLE_AUTOMATIONS)
            if not enabled:
                await asyncio.sleep(1)
                continue
            resp = await redis.xreadgroup(group, consumer, {settings.ALERTS_STREAM: ">"}, count=50, block=1000)
        except Exception as exc:
            LOG.info("automations read failed err=%s", exc)
            await asyncio.sleep(1)
            continue
        if not resp:
            continue
        to_ack: list[str] = []
        for _stream, msgs in resp:
            for msg_id, fields in msgs:
                to_ack.append(msg_id)
                # normalize alert
                alert = {
                    "id": msg_id,
                    "os": fields.get("os"),
                    "issue_key": fields.get("issue_key"),
                    "failure_type": fields.get("failure_type") or "",
                    "confidence": fields.get("confidence") or "",
                    "result": {},
                }
                try:
                    if fields.get("result"):
                        alert["result"] = json.loads(fields.get("result") or "{}")
                except Exception:
                    pass
                for rule in (rules.get("rules") or []):
                    try:
                        if not _match(rule, alert):
                            continue
                        key = alert.get("issue_key") or alert.get("id")
                        if not await _cooldown_guard(rule.get("id") or "rule", str(key), rule.get("cooldown") or "15m"):
                            continue
                        provider_name = (rule.get("action") or {}).get("provider") or ""
                        provider = PROVIDERS.get(provider_name)
                        if not provider:
                            continue
                        if _dry_run:
                            LOG.info("[dry-run] would trigger provider=%s rule=%s alert=%s", provider_name, rule.get("id"), alert.get("id"))
                        else:
                            await provider((rule.get("action") or {}).get("params") or {}, alert)
                        _total_triggered += 1
                        _provider_counts[provider_name] = _provider_counts.get(provider_name, 0) + 1
                        from datetime import datetime, timezone
                        _last_trigger_iso = datetime.now(timezone.utc).isoformat()
                    except Exception as exc:
                        LOG.info("automation exec failed rule=%s err=%s", rule.get("id"), exc)
        if to_ack:
            with contextlib.suppress(Exception):
                await redis.xack(settings.ALERTS_STREAM, group, *to_ack)


def attach_automations(app: FastAPI) -> None:
    async def _run_forever() -> None:
        backoff = 1.0
        while True:
            try:
                await run_automations()
            except Exception as exc:
                LOG.info("automations crashed err=%s; restarting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    @app.on_event("startup")
    async def startup_event() -> None:
        LOG.info("starting automations in dedicated thread")
        loop = asyncio.new_event_loop()

        def _runner() -> None:
            asyncio.set_event_loop(loop)
            loop.create_task(_run_forever())
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="automations-thread", daemon=True)
        thread.start()
        app.state.automations_loop = loop
        app.state.automations_thread = thread

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        LOG.info("stopping automations thread")
        loop = getattr(app.state, "automations_loop", None)
        thread = getattr(app.state, "automations_thread", None)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)



