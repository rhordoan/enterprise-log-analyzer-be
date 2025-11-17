from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.streams.producers.base import ProducerPlugin
from app.streams.producers.registry import register
from app.streams.utils import STREAM_NAME, safe_xadd, wait_for_redis


LOG = logging.getLogger(__name__)


class SCOMRestProducer(ProducerPlugin):
    name = "scom"

    def __init__(self, config: Dict[str, Any]):
        # Config:
        # {
        #   "base_url": "https://scom-server",
        #   "domain": "CONTOSO", "username": "svc", "password": "***",
        #   "verify_ssl": true,
        #   "poll_seconds": 30,
        #   "alerts_path": "/OperationsManager/data/alert",
        #   "perf_path": "/OperationsManager/data/performance",
        #   "events_path": "/OperationsManager/data/event",
        #   "criteria": {
        #       "alerts": "LastModified > '2025-01-01T00:00:00Z'",
        #       "perf": "...",
        #       "events": "..."
        #   }
        # }
        self.base_url: str = str(config.get("base_url") or "").rstrip("/")
        self.domain: str = str(config.get("domain") or "")
        self.username: str = str(config.get("username") or "")
        self.password: str = str(config.get("password") or "")
        self.verify_ssl: bool = bool(config.get("verify_ssl", True))
        self.poll_seconds: float = float(config.get("poll_seconds", 30))
        self.alerts_path: str = str(config.get("alerts_path") or "/OperationsManager/data/alert")
        self.perf_path: str = str(config.get("perf_path") or "/OperationsManager/data/performance")
        self.events_path: str = str(config.get("events_path") or "/OperationsManager/data/event")
        self.criteria: Dict[str, str] = dict(config.get("criteria") or {})
        self._stop = False
        parsed = urlparse(self.base_url)
        self._src_prefix = f"scom:{parsed.hostname or 'unknown'}"
        self._client: Optional[httpx.AsyncClient] = None
        self._csrf_header: Dict[str, str] = {}

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(verify=self.verify_ssl, timeout=None)
        return self._client

    async def _authenticate(self) -> bool:
        """SCOM auth handshake: POST /OperationsManager/authenticate with base64 body and keep cookies."""
        try:
            client = await self._ensure_client()
            # Body format typically: "(AuthenticationMode):domain\\username:password" base64
            # Use "Network" (Kerberos/NTLM) mode string for compatibility
            body_raw = f"(Network):{self.domain}\\{self.username}:{self.password}".encode("utf-8")
            encoded = base64.b64encode(body_raw).decode("ascii")
            auth_url = f"{self.base_url}/OperationsManager/authenticate"
            resp = await client.post(auth_url, headers={"Content-Type": "application/json; charset=utf-8"}, content=json.dumps(encoded))
            if resp.status_code // 100 != 2:
                LOG.info("scom: auth failed status=%s", resp.status_code)
                return False
            # Initialize CSRF token if required (SCOM 2019+)
            try:
                init_url = f"{self.base_url}/OperationsManager"
                init = await client.get(init_url)
                xsrf = init.headers.get("X-CSRF-Token") or init.headers.get("x-csrf-token")
                if xsrf:
                    self._csrf_header = {"X-CSRF-Token": xsrf}
            except Exception:  # noqa: BLE001
                self._csrf_header = {}
            return True
        except Exception as exc:  # noqa: BLE001
            LOG.info("scom: authenticate error err=%s", exc)
            return False

    async def _post_query(self, path: str, criteria: Optional[str]) -> List[Any]:
        items: List[Any] = []
        try:
            client = await self._ensure_client()
            url = f"{self.base_url}{path}"
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if self._csrf_header:
                headers.update(self._csrf_header)
            data = json.dumps(criteria) if criteria else json.dumps("")
            resp = await client.post(url, headers=headers, content=data)
            if resp.status_code == 401:
                # re-authenticate once
                ok = await self._authenticate()
                if not ok:
                    return []
                resp = await client.post(url, headers=headers, content=data)
            resp.raise_for_status()
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = []
            # SCOM often returns {"items":[...]} or direct arrays
            if isinstance(body, dict) and "items" in body:
                arr = body.get("items") or []
                if isinstance(arr, list):
                    items = arr
            elif isinstance(body, list):
                items = body
        except Exception as exc:  # noqa: BLE001
            LOG.info("scom: query failed path=%s err=%s", path, exc)
        return items

    async def _poll_once(self) -> None:
        # Alerts
        alerts = await self._post_query(self.alerts_path, self.criteria.get("alerts"))
        for it in alerts:
            try:
                await safe_xadd(
                    STREAM_NAME,
                    {
                        "source": self._src_prefix,
                        "line": json.dumps({"type": "alert", **(it if isinstance(it, dict) else {"value": it})}, ensure_ascii=False),
                    },
                )
            except Exception:
                pass
        # Performance
        perfs = await self._post_query(self.perf_path, self.criteria.get("perf"))
        for it in perfs:
            try:
                await safe_xadd(
                    STREAM_NAME,
                    {
                        "source": self._src_prefix,
                        "line": json.dumps({"type": "performance", **(it if isinstance(it, dict) else {"value": it})}, ensure_ascii=False),
                    },
                )
            except Exception:
                pass
        # Events
        events = await self._post_query(self.events_path, self.criteria.get("events"))
        for it in events:
            try:
                await safe_xadd(
                    STREAM_NAME,
                    {
                        "source": self._src_prefix,
                        "line": json.dumps({"type": "event", **(it if isinstance(it, dict) else {"value": it})}, ensure_ascii=False),
                    },
                )
            except Exception:
                pass

    async def run(self) -> None:
        await wait_for_redis()
        if not (self.base_url and self.username and self.password and self.domain):
            LOG.info("scom: missing configuration; idle")
            while not self._stop:
                await asyncio.sleep(60)
            return
        ok = await self._authenticate()
        if not ok:
            LOG.info("scom: initial authentication failed; will retry later")
        backoff = 1.0
        while not self._stop:
            try:
                await self._poll_once()
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001
                LOG.info("scom: poll failed err=%s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            await asyncio.sleep(self.poll_seconds)

    async def shutdown(self) -> None:
        self._stop = True
        try:
            if self._client is not None:
                await self._client.aclose()
        except Exception:
            pass


@register("scom")
def _factory(cfg: dict):
    return SCOMRestProducer(cfg)





