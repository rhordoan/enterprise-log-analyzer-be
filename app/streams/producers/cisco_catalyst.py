from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.streams.producers.base import ProducerPlugin
from app.streams.producers.registry import register
from app.streams.utils import STREAM_NAME, safe_xadd, wait_for_redis


LOG = logging.getLogger(__name__)


class CiscoCatalystProducer(ProducerPlugin):
    name = "catalyst"

    def __init__(self, config: Dict[str, Any]):
        # Config example:
        # {
        #   "base_url": "https://dnac",
        #   "username": "admin",
        #   "password": "***",
        #   "verify_ssl": true,
        #   "poll_seconds": 30,
        #   "auth_path": "/dna/system/api/v1/auth/token",
        #   "health_paths": {
        #       "network": "/dna/intent/api/v1/network-health",
        #       "client": "/dna/intent/api/v1/client-health",
        #       "device": "/dna/intent/api/v1/device-health"
        #   },
        #   "events_path": "/dna/intent/api/v1/events"
        # }
        self.base_url: str = str(config.get("base_url") or "").rstrip("/")
        self.username: str = str(config.get("username") or "")
        self.password: str = str(config.get("password") or "")
        self.verify_ssl: bool = bool(config.get("verify_ssl", True))
        self.poll_seconds: float = float(config.get("poll_seconds", 30))
        self.auth_path: str = str(config.get("auth_path") or "/dna/system/api/v1/auth/token")
        hp = config.get("health_paths") or {}
        self.health_paths: Dict[str, str] = {
            "network": str(hp.get("network") or "/dna/intent/api/v1/network-health"),
            "client": str(hp.get("client") or "/dna/intent/api/v1/client-health"),
            "device": str(hp.get("device") or "/dna/intent/api/v1/device-health"),
        }
        self.events_path: str = str(config.get("events_path") or "/dna/intent/api/v1/events")
        parsed = urlparse(self.base_url)
        self._src_prefix = f"catalyst:{parsed.hostname or 'unknown'}"
        self._token: Optional[str] = None
        self._stop = False

    async def _get_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(verify=self.verify_ssl, timeout=None)

    async def _auth(self) -> Optional[str]:
        if not (self.base_url and self.username and self.password):
            return None
        try:
            async with await self._get_client() as client:
                url = f"{self.base_url}{self.auth_path}"
                resp = await client.post(url, auth=(self.username, self.password))
                resp.raise_for_status()
                # Token can be in header or JSON {"Token":"..."}
                token = resp.headers.get("X-Auth-Token")
                if not token:
                    try:
                        token = (resp.json() or {}).get("Token")
                    except Exception:
                        token = None
                self._token = token
                return token
        except Exception as exc:  # noqa: BLE001
            LOG.info("catalyst: auth failed err=%s", exc)
            return None

    async def _get(self, path: str) -> Any:
        if not self._token:
            await self._auth()
        headers = {"X-Auth-Token": self._token or ""}
        url = f"{self.base_url}{path}"
        try:
            async with await self._get_client() as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 401:
                    await self._auth()
                    headers = {"X-Auth-Token": self._token or ""}
                    resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                try:
                    return resp.json()
                except Exception:
                    return resp.text
        except Exception as exc:  # noqa: BLE001
            LOG.info("catalyst: request failed path=%s err=%s", path, exc)
            return None

    async def run(self) -> None:
        await wait_for_redis()
        if not (self.base_url and self.username and self.password):
            LOG.info("catalyst: missing configuration; idle")
            while not self._stop:
                await asyncio.sleep(60)
            return
        backoff = 1.0
        while not self._stop:
            try:
                # Health domains
                for domain, path in self.health_paths.items():
                    body = await self._get(path)
                    if body is None:
                        continue
                    items = body if isinstance(body, list) else [body]
                    for it in items:
                        payload = {"type": f"health_{domain}", **(it if isinstance(it, dict) else {"value": it})}
                        await safe_xadd(STREAM_NAME, {"source": self._src_prefix, "line": json.dumps(payload, ensure_ascii=False)})
                # Events (optional)
                if self.events_path:
                    body = await self._get(self.events_path)
                    if body is not None:
                        items = body if isinstance(body, list) else [body]
                        for it in items:
                            payload = {"type": "event", **(it if isinstance(it, dict) else {"value": it})}
                            await safe_xadd(STREAM_NAME, {"source": self._src_prefix, "line": json.dumps(payload, ensure_ascii=False)})
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001
                LOG.info("catalyst: poll failed err=%s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            await asyncio.sleep(self.poll_seconds)

    async def shutdown(self) -> None:
        self._stop = True


@register("catalyst")
def _factory(cfg: dict):
    return CiscoCatalystProducer(cfg)






