from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta

import httpx

from app.streams.producers.base import ProducerPlugin
from app.streams.producers.registry import register
from app.streams.utils import STREAM_NAME, safe_xadd, wait_for_redis


LOG = logging.getLogger(__name__)


class RedfishProducer(ProducerPlugin):
    name = "redfish"

    def __init__(self, config: Dict[str, Any]):
        self.mode: str = str(config.get("mode") or "direct")  # "direct" | "ome"
        self.verify_ssl: bool = bool(config.get("verify_ssl", True))
        self.ca_bundle_path: str | None = str(config.get("ca_bundle_path") or "") or None
        self.interval: float = float(config.get("poll_interval_sec", 60))
        self.since_minutes: int = int(config.get("since_minutes", 30))
        self.auth_user: str = str((config.get("auth") or {}).get("username") or "")
        self.auth_pass: str = str((config.get("auth") or {}).get("password") or "")
        self.hosts: List[str] = list(config.get("hosts") or [])
        self.ome_base_url: str = str(config.get("ome_base_url") or "")
        self._source_id: Optional[int] = int(config.get("_source_id")) if config.get("_source_id") is not None else None
        self._stop = False
        # cursors by key (host or device id)
        self._last_log_time: Dict[str, str] = {}

    async def run(self) -> None:
        await wait_for_redis()
        if self.mode not in {"direct", "ome"}:
            LOG.info("redfish: invalid mode=%s", self.mode)
            return
        LOG.info(
            "redfish: starting mode=%s verify_ssl=%s ca_bundle=%s hosts=%d ome_url=%s",
            self.mode,
            self.verify_ssl,
            bool(self.ca_bundle_path),
            len(self.hosts or []),
            (self.ome_base_url or "-") if self.mode == "ome" else "-",
        )
        # Avoid tight restart loop if misconfigured
        if self.mode == "direct" and not self.hosts:
            LOG.info("redfish: no hosts configured; idle")
            while not self._stop:
                await asyncio.sleep(60)
            return
        if self.mode == "ome" and not self.ome_base_url:
            LOG.info("redfish: ome_base_url missing; idle")
            while not self._stop:
                await asyncio.sleep(60)
            return
        tasks: List[asyncio.Task] = []
        if self.mode == "direct":
            for host in self.hosts:
                tasks.append(asyncio.create_task(self._poll_direct_host(host)))
        else:
            tasks.append(asyncio.create_task(self._poll_ome_aggregator()))
        await asyncio.gather(*tasks)

    async def shutdown(self) -> None:
        self._stop = True

    # --- helpers ---
    def _auth(self) -> Optional[Tuple[str, str]]:
        if self.auth_user:
            return (self.auth_user, self.auth_pass)
        return None

    async def _emit_log_line(self, key: str, msg: str) -> None:
        try:
            await safe_xadd(
                STREAM_NAME,
                {
                    "source": f"redfish_log:{key}",
                    "line": msg,
                    **({"source_id": str(self._source_id)} if self._source_id is not None else {}),
                },
            )
        except Exception as exc:  # noqa: BLE001
            LOG.info("redfish: failed to emit log key=%s err=%s", key, exc)

    async def _emit_metric_payload(self, host: str, payload: Dict[str, Any]) -> None:
        try:
            await safe_xadd(
                STREAM_NAME,
                {
                    "source": f"redfish:{host}",
                    "line": json.dumps(payload, ensure_ascii=False),
                    **({"source_id": str(self._source_id)} if self._source_id is not None else {}),
                },
            )
        except Exception as exc:  # noqa: BLE001
            LOG.info("redfish: failed to emit metrics host=%s err=%s", host, exc)

    async def _fetch_json(self, client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse_time(value: str) -> Optional[datetime]:
        if not value:
            return None
        s = value.strip()
        try:
            # Handle trailing Z
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)
        except Exception:
            return None

    async def _poll_direct_host(self, base: str) -> None:
        # normalize base like https://host
        base = base.rstrip("/")
        auth = self._auth()
        verify_arg: Any = self.ca_bundle_path if self.ca_bundle_path else self.verify_ssl
        async with httpx.AsyncClient(verify=verify_arg, timeout=None, auth=auth) as client:
            while not self._stop:
                try:
                    new_logs = 0
                    metrics_payloads = 0
                    # Logs: Managers/*/LogServices/*/Entries
                    try:
                        mgrs = await self._fetch_json(client, f"{base}/redfish/v1/Managers")
                        try:
                            LOG.info("redfish: host=%s managers=%d", base, len(mgrs.get("Members") or []))
                        except Exception:
                            pass
                        for m in (mgrs.get("Members") or []):
                            mid = m.get("@odata.id")
                            if not isinstance(mid, str):
                                continue
                            try:
                                ls = await self._fetch_json(client, f"{base}{mid}/LogServices")
                                for svc in (ls.get("Members") or []):
                                    sid = svc.get("@odata.id")
                                    if not isinstance(sid, str):
                                        continue
                                    entries_url = f"{base}{sid}/Entries"
                                    new_logs += await self._collect_and_emit_entries(client, base, entries_url)
                            except Exception:
                                continue
                    except Exception as exc:
                        LOG.info("redfish: managers fetch failed host=%s err=%s", base, exc)

                    # Metrics: Chassis/* Thermal, Power
                    try:
                        ch = await self._fetch_json(client, f"{base}/redfish/v1/Chassis")
                        try:
                            LOG.info("redfish: host=%s chassis=%d", base, len(ch.get("Members") or []))
                        except Exception:
                            pass
                        for c in (ch.get("Members") or []):
                            cid = c.get("@odata.id")
                            if not isinstance(cid, str):
                                continue
                            # Thermal
                            try:
                                thermal = await self._fetch_json(client, f"{base}{cid}/Thermal")
                                await self._emit_metric_payload(base, {"host": base, "kind": "thermal", "body": thermal})
                                metrics_payloads += 1
                            except Exception:
                                pass
                            # Power
                            try:
                                power = await self._fetch_json(client, f"{base}{cid}/Power")
                                await self._emit_metric_payload(base, {"host": base, "kind": "power", "body": power})
                                metrics_payloads += 1
                            except Exception:
                                pass
                    except Exception as exc:
                        LOG.info("redfish: chassis fetch failed host=%s err=%s", base, exc)
                    try:
                        LOG.info("redfish: host=%s poll logs=%d metrics_payloads=%d", base, new_logs, metrics_payloads)

                    except Exception:
                        pass
                except Exception as exc:  # noqa: BLE001
                    LOG.info("redfish: poll error host=%s err=%s", base, exc)
                await asyncio.sleep(self.interval)

    async def _poll_ome_aggregator(self) -> None:
        base = (self.ome_base_url or "").rstrip("/")
        if not base:
            LOG.info("redfish: ome mode requires ome_base_url")
            return
        auth = self._auth()
        verify_arg: Any = self.ca_bundle_path if self.ca_bundle_path else self.verify_ssl
        async with httpx.AsyncClient(verify=verify_arg, timeout=None, auth=auth) as client:
            while not self._stop:
                try:
                    new_logs = 0
                    metrics_payloads = 0
                    # Discover systems via OME aggregator
                    systems = []
                    try:
                        sys_idx = await self._fetch_json(client, f"{base}/redfish/v1/Systems")
                        systems = [s.get("@odata.id") for s in (sys_idx.get("Members") or []) if isinstance(s.get("@odata.id"), str)]
                    except Exception:
                        systems = []
                    managers = []
                    try:
                        man_idx = await self._fetch_json(client, f"{base}/redfish/v1/Managers")
                        managers = [s.get("@odata.id") for s in (man_idx.get("Members") or []) if isinstance(s.get("@odata.id"), str)]
                    except Exception:
                        managers = []
                    LOG.info("redfish: ome discovery systems=%d managers=%d", len(systems), len(managers))

                    # Logs via managers' LogServices
                    for mid in managers:
                        try:
                            ls = await self._fetch_json(client, f"{base}{mid}/LogServices")
                            for svc in (ls.get("Members") or []):
                                sid = svc.get("@odata.id")
                                if not isinstance(sid, str):
                                    continue
                                entries_url = f"{base}{sid}/Entries"
                                new_logs += await self._collect_and_emit_entries(client, mid, entries_url)
                        except Exception:
                            continue

                    # Also attempt logs via systems' LogServices (some OME setups expose these here)
                    for sid in systems:
                        try:
                            ls2 = await self._fetch_json(client, f"{base}{sid}/LogServices")
                            for svc in (ls2.get("Members") or []):
                                sid2 = svc.get("@odata.id")
                                if not isinstance(sid2, str):
                                    continue
                                entries_url = f"{base}{sid2}/Entries"
                                new_logs += await self._collect_and_emit_entries(client, sid, entries_url)
                        except Exception:
                            continue

                    # Metrics via systems/chassis if exposed by aggregator
                    for sid in systems:
                        # try associated chassis via navigation (best-effort)
                        try:
                            # common chassis path may not be directly linked from Systems; we try a few heuristics
                            # emit power/thermal if present under the same system path
                            try:
                                thermal = await self._fetch_json(client, f"{base}{sid}/Thermal")
                                await self._emit_metric_payload(sid, {"host": sid, "kind": "thermal", "body": thermal})
                                metrics_payloads += 1
                            except Exception:
                                pass
                            try:
                                power = await self._fetch_json(client, f"{base}{sid}/Power")
                                await self._emit_metric_payload(sid, {"host": sid, "kind": "power", "body": power})
                                metrics_payloads += 1
                            except Exception:
                                pass
                        except Exception:
                            continue
                    try:
                        LOG.info("redfish: ome poll logs=%d metrics_payloads=%d", new_logs, metrics_payloads)
                    except Exception:
                        pass
                except Exception as exc:  # noqa: BLE001
                    LOG.info("redfish: ome poll error err=%s", exc)
                await asyncio.sleep(self.interval)

    async def _collect_and_emit_entries(self, client: httpx.AsyncClient, key: str, entries_url: str) -> int:
        try:
            data = await self._fetch_json(client, entries_url)
        except Exception as exc:  # noqa: BLE001
            LOG.info("redfish: entries fetch failed key=%s url=%s err=%s", key, entries_url, exc)
            return 0
        members = data.get("Members") or []
        # Newest first or not guaranteed; sort by Created if available
        def _ts(x: Any) -> str:
            if isinstance(x, dict):
                v = x.get("Created") or x.get("CreatedDateTime") or ""
                return str(v)
            return ""
        members_sorted = sorted(members, key=_ts)
        last_key = f"{key}:{entries_url}"
        last_seen = self._last_log_time.get(last_key, "")
        newest = last_seen
        # Backfill window on first run
        threshold_dt: Optional[datetime] = None
        if not last_seen and self.since_minutes and self.since_minutes > 0:
            threshold_dt = datetime.now(timezone.utc) - timedelta(minutes=int(self.since_minutes))
        emitted = 0
        for item in members_sorted:
            if not isinstance(item, dict):
                continue
            created = str(item.get("Created") or item.get("CreatedDateTime") or "")
            message = str(item.get("Message") or item.get("LogEntry") or item.get("Description") or "").strip()
            if not message:
                continue
            if created and last_seen and created <= last_seen:
                continue
            if threshold_dt is not None:
                cdt = self._parse_time(created)
                if cdt is not None and cdt.replace(tzinfo=cdt.tzinfo or timezone.utc) < threshold_dt:
                    # Older than backfill window on first run; skip
                    continue
            line = f"{created} {message}".strip()
            await self._emit_log_line(key, line)
            emitted += 1
            if created and created > newest:
                newest = created
        if newest:
            self._last_log_time[last_key] = newest
        if emitted:
            LOG.info("redfish: key=%s emitted_log_entries=%d", key, emitted)
        return emitted


@register("redfish")
def _factory(cfg: Dict[str, Any]):
    return RedfishProducer(cfg)


