from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress

from fastapi import FastAPI
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.data_source import DataSource
from app.streams.producers.registry import get_factory

# Ensure built-in producers are imported so they register
from app.streams.producers import filetail as _filetail  # noqa: F401
from app.streams.producers import splunk as _splunk  # noqa: F401
from app.streams.producers import datadog as _datadog  # noqa: F401
from app.streams.producers import thousandeyes as _thousandeyes  # noqa: F401
from app.streams.producers import snmp as _snmp  # noqa: F401
from app.streams.producers import http_poller as _dcim_http  # noqa: F401
from app.streams.producers import redfish as _redfish  # noqa: F401
from app.streams.producers import dell_ome as _dell_ome  # noqa: F401
from app.streams.producers import scom as _scom  # noqa: F401
from app.streams.producers import squaredup as _squaredup  # noqa: F401
from app.streams.producers import cisco_catalyst as _catalyst  # noqa: F401
from app.streams.producers import bluecat as _bluecat  # noqa: F401


LOG = logging.getLogger(__name__)


class ProducerManager:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.tasks: dict[int, asyncio.Task] = {}
        self.instances: dict[int, object] = {}
        self._heartbeat_interval_seconds: float = 60.0

    async def _run_with_restart(self, source_id: int, instance: object) -> None:
        backoff = 1.0
        while True:
            try:
                await instance.run()  # type: ignore[attr-defined]
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                LOG.info(
                    "producer id=%s crashed err=%s; restarting in %.1fs",
                    source_id,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    def ensure_loop(self) -> None:
        if self.loop is not None:
            return
        self.loop = asyncio.new_event_loop()

        def _runner() -> None:
            asyncio.set_event_loop(self.loop)
            # Start heartbeat task in the producers loop
            self.loop.create_task(self._heartbeat())
            self.loop.run_forever()

        self.thread = threading.Thread(target=_runner, name="producers-thread", daemon=True)
        self.thread.start()

    async def _heartbeat(self) -> None:
        """Log active producer count and identities periodically."""
        while True:
            try:
                active_ids = list(self.tasks.keys())
                num_active = len(active_ids)
                details: list[str] = []
                for rid in active_ids:
                    inst = self.instances.get(rid)
                    if inst is None:
                        details.append(f"{rid}:-")
                    else:
                        ptype = getattr(inst, "name", inst.__class__.__name__)
                        details.append(f"{rid}:{ptype}")
                LOG.info(
                    "producers heartbeat active=%d running=%s",
                    num_active,
                    ", ".join(details) if details else "-",
                )
                await asyncio.sleep(self._heartbeat_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                LOG.info("producers heartbeat error=%s", exc)
                await asyncio.sleep(self._heartbeat_interval_seconds)

    def start(self, source_id: int, type_: str, config: dict) -> None:
        if self.loop is None:
            self.ensure_loop()
        if source_id in self.tasks:
            return
        # Resolve factory; skip unknown types gracefully (e.g., HTTP-ingested kinds like 'telegraf')
        try:
            factory = get_factory(type_)
        except KeyError:
            LOG.info("skip starting unknown producer type=%s id=%s (no plugin registered)", type_, source_id)
            return
        cfg = dict(config)
        # Pass source_id to the plugin for downstream enrichment
        cfg["_source_id"] = source_id
        cfg["_type"] = type_
        instance = factory(cfg)
        task = self.loop.create_task(self._run_with_restart(source_id, instance))  # type: ignore[arg-type]
        self.instances[source_id] = instance
        self.tasks[source_id] = task
        LOG.info("started producer id=%s type=%s", source_id, type_)

    async def stop(self, source_id: int) -> None:
        inst = self.instances.pop(source_id, None)
        task = self.tasks.pop(source_id, None)
        if inst is not None:
            with suppress(Exception):
                await inst.shutdown()  # type: ignore[attr-defined]
        if task is not None:
            task.cancel()
        LOG.info("stopped producer id=%s", source_id)

    async def reconcile_all(self) -> None:
        async with AsyncSessionLocal() as db:  # type: AsyncSession
            rows = (
                await db.execute(select(DataSource).where(DataSource.enabled == True))  # noqa: E712
            ).scalars().all()
        active_ids = {r.id for r in rows}
        # stop removed
        for rid in list(self.tasks.keys()):
            if rid not in active_ids:
                await self.stop(rid)
        # start new
        for r in rows:
            if r.id not in self.tasks:
                # Skip non-plugin types that are ingested via HTTP endpoints
                if r.type in {"telegraf"}:
                    LOG.info("not starting producer for type=%s id=%s (handled via ingestion)", r.type, r.id)
                    continue
                self.start(r.id, r.type, r.config)


manager = ProducerManager()


def attach_producers(app: FastAPI) -> None:
    @app.on_event("startup")
    async def _startup() -> None:
        manager.ensure_loop()
        await manager.reconcile_all()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        for rid in list(manager.tasks.keys()):
            with suppress(Exception):
                await manager.stop(rid)
        if manager.loop is not None:
            manager.loop.call_soon_threadsafe(manager.loop.stop)
        if manager.thread is not None:
            manager.thread.join(timeout=5)


