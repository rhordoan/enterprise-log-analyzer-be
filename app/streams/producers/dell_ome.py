from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.streams.producers.base import ProducerPlugin
from app.streams.producers.registry import register
from app.streams.utils import STREAM_NAME, safe_xadd, wait_for_redis


LOG = logging.getLogger(__name__)


class DellOMELogs(ProducerPlugin):
    name = "dell_ome"

    def __init__(self, config: Dict[str, Any]):
        self.verify_ssl: bool = bool(config.get("verify_ssl", True))
        self.ca_bundle_path: str | None = str(config.get("ca_bundle_path") or "") or None
        self.interval: float = float(config.get("poll_interval_sec", 60))
        self.since_minutes: int = int(config.get("since_minutes", 30))
        self.auth_user: str = str((config.get("auth") or {}).get("username") or "")
        self.auth_pass: str = str((config.get("auth") or {}).get("password") or "")
        self.base_url: str = str(config.get("ome_base_url") or "").rstrip("/")
        self.device_ids: List[int] = list(config.get("device_ids") or [])
        # Optional: devices discovery via a pasted OME link to list devices (e.g. /api/DeviceService/Devices?$filter=...)
        self.devices_url: str | None = str(config.get("devices_url") or "") or None
        self.discovery_interval: float = float(config.get("discovery_interval_sec", 300))
        self._source_id: Optional[int] = int(config.get("_source_id")) if config.get("_source_id") is not None else None
        self._stop = False
        self._last_time: Dict[int, str] = {}
        self._tasks: Dict[int, asyncio.Task] = {}

    async def run(self) -> None:
        await wait_for_redis()
        if not self.base_url:
            LOG.info("dell_ome: missing ome_base_url")
            return
        # Optionally discover devices from a provided OME link
        if (not self.device_ids) and self.devices_url:
            try:
                first = await self._discover_devices()
                if first:
                    self.device_ids = first
                    LOG.info("dell_ome: discovery found devices=%d", len(first))
                else:
                    LOG.info("dell_ome: discovery found no devices from devices_url")
            except Exception as exc:
                LOG.info("dell_ome: discovery failed err=%s", exc)

        if not self.device_ids and not self.devices_url:
            LOG.info("dell_ome: no device_ids configured and no devices_url provided")
            # idle loop
            while not self._stop:
                await asyncio.sleep(60)
            return
        try:
            LOG.info(
                "dell_ome: starting base_url=%s devices=%d verify_ssl=%s ca_bundle=%s interval=%ss",
                self.base_url,
                len(self.device_ids),
                self.verify_ssl,
                bool(self.ca_bundle_path),
                self.interval,
            )
        except Exception:
            pass
        # Start initial tasks
        for dev_id in self.device_ids:
            if dev_id not in self._tasks:
                self._tasks[dev_id] = asyncio.create_task(self._poll_device(dev_id))

        # If devices_url is set, periodically re-discover and add new devices
        if self.devices_url:
            while not self._stop:
                try:
                    await asyncio.sleep(self.discovery_interval)
                    ids = await self._discover_devices()
                    new_ids = [i for i in ids if i not in self._tasks]
                    for dev_id in new_ids:
                        LOG.info("dell_ome: discovered new device_id=%s; starting poller", dev_id)
                        self._tasks[dev_id] = asyncio.create_task(self._poll_device(dev_id))
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    LOG.info("dell_ome: periodic discovery failed err=%s", exc)
        else:
            # No discovery loop; just wait on existing tasks
            await asyncio.gather(*list(self._tasks.values()))

    async def shutdown(self) -> None:
        self._stop = True

    def _auth(self) -> Optional[Tuple[str, str]]:
        if self.auth_user:
            return (self.auth_user, self.auth_pass)
        return None

    async def _discover_devices(self) -> List[int]:
        """Fetch device IDs from an OME devices listing link.

        Expects a JSON object with a 'value' list; items containing an integer Id field.
        """
        if not self.devices_url:
            return []
        url = self.devices_url
        verify_arg: Any = self.ca_bundle_path if self.ca_bundle_path else self.verify_ssl
        auth = self._auth()
        async with httpx.AsyncClient(verify=verify_arg, timeout=None, auth=auth) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            arr = data if isinstance(data, list) else (data.get("value") if isinstance(data, dict) else [])
            ids: List[int] = []
            for it in (arr or []):
                if not isinstance(it, dict):
                    continue
                try:
                    vid = it.get("Id") or it.get("DeviceId") or it.get("id")
                    if isinstance(vid, int):
                        ids.append(int(vid))
                    else:
                        # sometimes Id comes as string
                        if isinstance(vid, str) and vid.isdigit():
                            ids.append(int(vid))
                except Exception:
                    continue
            return ids

    async def _poll_device(self, device_id: int) -> None:
        url = f"{self.base_url}/api/DeviceService/Devices({device_id})/HardwareLogs"
        auth = self._auth()
        verify_arg: Any = self.ca_bundle_path if self.ca_bundle_path else self.verify_ssl
        async with httpx.AsyncClient(verify=verify_arg, timeout=None, auth=auth) as client:
            while not self._stop:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
                    items = data if isinstance(data, list) else (data.get("value") if isinstance(data, dict) else [])
                    def _ts(x: Any) -> str:
                        if isinstance(x, dict):
                            return str(x.get("Created") or x.get("CreatedDateTime") or x.get("TimeStamp") or "")
                        return ""
                    items_sorted = sorted(items or [], key=_ts)
                    last_seen = self._last_time.get(device_id, "")
                    newest = last_seen
                    emitted = 0
                    for it in items_sorted:
                        if not isinstance(it, dict):
                            continue
                        created = str(it.get("Created") or it.get("CreatedDateTime") or it.get("TimeStamp") or "")
                        message = str(it.get("Message") or it.get("LogEntry") or it.get("Description") or "").strip()
                        if not message:
                            continue
                        if created and last_seen and created <= last_seen:
                            continue
                        line = f"{created} {message}".strip()
                        await safe_xadd(
                            STREAM_NAME,
                            {
                                "source": f"ome_log:{device_id}",
                                "line": line,
                                **({"source_id": str(self._source_id)} if self._source_id is not None else {}),
                            },
                        )
                        emitted += 1
                        if created and created > newest:
                            newest = created
                    if newest:
                        self._last_time[device_id] = newest
                    fetched = len(items_sorted)
                    if emitted:
                        LOG.info("dell_ome: device_id=%s emitted_log_entries=%d (fetched=%d)", device_id, emitted, fetched)
                    else:
                        LOG.info("dell_ome: device_id=%s no new entries (fetched=%d last_seen=%s)", device_id, fetched, last_seen or "-")
                except Exception as exc:  # noqa: BLE001
                    LOG.info("dell_ome: poll error device_id=%s err=%s", device_id, exc)
                await asyncio.sleep(self.interval)


@register("dell_ome")
def _factory(cfg: Dict[str, Any]):
    return DellOMELogs(cfg)


