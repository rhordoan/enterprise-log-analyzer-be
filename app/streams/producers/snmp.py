from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.streams.producers.base import ProducerPlugin
from app.streams.producers.registry import register
from app.streams.utils import STREAM_NAME, safe_xadd, wait_for_redis


LOG = logging.getLogger(__name__)


def _import_puresnmp():
    try:
        # puresnmp is synchronous; we will run calls in a thread
        import puresnmp

        return puresnmp
    except Exception:  # noqa: BLE001
        return None


class SNMPProducer(ProducerPlugin):
    name = "snmp"

    def __init__(self, config: dict[str, Any]):
        # Example config:
        # {
        #   "hosts": [{"host": "10.0.0.1", "community": "public", "port": 161}],
        #   "oids": ["1.3.6.1.2.1.1.3.0", "1.3.6.1.2.1.2.2.1.8.1"],
        #   "poll_interval_sec": 30,
        #   "timeout_sec": 3
        # }
        self.hosts: list[dict[str, Any]] = list(config.get("hosts") or [])
        self.oids: list[str] = list(config.get("oids") or [])
        self.interval: float = float(config.get("poll_interval_sec", 30))
        self.timeout: float = float(config.get("timeout_sec", 3))
        self._stop = False
        self._source_id: int | None = int(config.get("_source_id")) if config.get("_source_id") is not None else None
        self._snmp = _import_puresnmp()

    async def _get_oid(self, client: Any, oid: str) -> Any:
        # puresnmp uses tuples for OIDs sometimes; accept string
        def _call():
            try:
                return client.get(oid)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                return exc

        return await asyncio.to_thread(_call)

    async def _poll_host(self, hcfg: dict[str, Any]) -> None:
        host: str = str(hcfg.get("host") or "")
        community: str = str(hcfg.get("community") or "public")
        port: int = int(hcfg.get("port") or 161)
        if not host:
            return
        if not self._snmp:
            LOG.info("snmp: puresnmp not installed; idle")
            while not self._stop:
                await asyncio.sleep(60)
            return
        # Build client lazily per poll to avoid stale sockets
        puresnmp = self._snmp
        while not self._stop:
            try:
                client = puresnmp.Client(host, community=community, port=port, timeout=self.timeout)
                for oid in self.oids:
                    res = await self._get_oid(client, oid)
                    if isinstance(res, Exception):
                        LOG.info("snmp: host=%s oid=%s err=%s", host, oid, res)
                        continue
                    payload = {
                        "host": host,
                        "port": port,
                        "community": "***",
                        "oid": oid,
                        "value": str(res),
                    }
                    await safe_xadd(
                        STREAM_NAME,
                        {
                            "source": f"snmp:{host}",
                            "line": json.dumps(payload, ensure_ascii=False),
                            **({"source_id": str(self._source_id)} if self._source_id is not None else {}),
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                LOG.info("snmp: poll error host=%s err=%s", host, exc)
            # Sleep between host polls
            await asyncio.sleep(self.interval)

    async def run(self) -> None:
        await wait_for_redis()
        if not self.hosts or not self.oids:
            LOG.info("snmp: no hosts or oids configured; idle")
            while not self._stop:
                await asyncio.sleep(60)
            return
        tasks = [asyncio.create_task(self._poll_host(h)) for h in self.hosts]
        await asyncio.gather(*tasks)

    async def shutdown(self) -> None:
        self._stop = True


@register("snmp")
def _factory(cfg: dict):
    return SNMPProducer(cfg)


