from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

from app.streams.producers.base import ProducerPlugin
from app.streams.producers.registry import register
from app.streams.utils import STREAM_NAME, safe_xadd, wait_for_redis


LOG = logging.getLogger(__name__)


class ThousandEyes(ProducerPlugin):
    name = "thousandeyes"

    def __init__(self, cfg: dict):
        base = (cfg.get("base_url") or "").rstrip("/")
        # Default to alerts v6 endpoint with window (e.g., 5m). Allow override via path.
        path = cfg.get("path") or "/v6/alerts.json"
        self.url = f"{base}{path}"
        self.window = cfg.get("window") or "5m"
        self.poll_interval_sec: int = int(cfg.get("poll_interval_sec") or 15)
        self.verify_ssl: bool = bool(cfg.get("verify_ssl", True))
        self.os_hint: str = (cfg.get("os") or "unknown").lower()

        # Auth: prefer Bearer token; fallback to X-TE-Auth-Token header if provided
        self.bearer_token: Optional[str] = cfg.get("bearer_token")
        self.api_token: Optional[str] = cfg.get("api_token")

        self._stop = False

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        if self.api_token:
            headers["X-TE-Auth-Token"] = self.api_token
        return headers

    async def _poll_once(self, client: httpx.AsyncClient) -> int:
        params = {"window": self.window}
        resp = await client.get(self.url, params=params, headers=self._headers())
        resp.raise_for_status()
        data: Dict[str, Any] = resp.json()
        alerts = data.get("alerts") or data.get("alert") or []
        count = 0
        for a in alerts:
            try:
                # Build a readable line
                rule = a.get("ruleName") or a.get("alertType") or "alert"
                test = a.get("testName") or a.get("testId") or ""
                sev = a.get("severity") or a.get("level") or ""
                msg = a.get("summary") or a.get("description") or ""
                line = " ".join(str(x) for x in [rule, test, sev, msg] if x)
                if not line:
                    line = str(a)
                source = f"thousandeyes:{self.os_hint}"
                await safe_xadd(STREAM_NAME, {"source": source, "line": line.strip()})
                count += 1
            except Exception as exc:  # noqa: BLE001
                LOG.info("thousandeyes: failed to emit log err=%s", exc)
        return count

    async def run(self) -> None:
        await wait_for_redis()
        if not self.url:
            LOG.info("thousandeyes: missing base_url/path; not starting")
            while not self._stop:
                await asyncio.sleep(60)
            return
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30) as client:
            while not self._stop:
                try:
                    n = await self._poll_once(client)
                    LOG.info("thousandeyes: fetched %d items", n)
                except Exception as exc:  # noqa: BLE001
                    LOG.info("thousandeyes poll failed err=%s", exc)
                await asyncio.sleep(self.poll_interval_sec)

    async def shutdown(self) -> None:
        self._stop = True


@register("thousandeyes")
def _factory(cfg: dict):
    return ThousandEyes(cfg)




