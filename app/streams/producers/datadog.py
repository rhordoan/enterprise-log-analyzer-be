from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx

from app.streams.producers.base import ProducerPlugin
from app.streams.producers.registry import register
from app.streams.utils import STREAM_NAME, safe_xadd, wait_for_redis


LOG = logging.getLogger(__name__)


class Datadog(ProducerPlugin):
    name = "datadog"

    def __init__(self, cfg: dict):
        # Required
        self.site: str = cfg.get("site") or "datadoghq.com"
        self.api_key: str = cfg.get("api_key") or ""
        self.app_key: str = cfg.get("app_key") or ""
        self.query: str = cfg.get("query") or "*"
        # Optional
        self.minutes_back: int = int(cfg.get("minutes_back") or 5)
        self.poll_interval_sec: int = int(cfg.get("poll_interval_sec") or 15)
        self.verify_ssl: bool = bool(cfg.get("verify_ssl", True))
        self.os_hint: str = (cfg.get("os") or "unknown").lower()
        self._stop = False
        self._since: Optional[datetime] = None

    def _headers(self) -> Dict[str, str]:
        return {
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
            "Content-Type": "application/json",
        }

    def _api_url(self) -> str:
        return f"https://api.{self.site}/api/v2/logs/events/search"

    async def _poll_once(self, client: httpx.AsyncClient) -> int:
        # Determine time window
        now = datetime.now(timezone.utc)
        if self._since is None:
            frm = now - timedelta(minutes=self.minutes_back)
        else:
            frm = self._since
        # Datadog expects RFC3339 strings
        params = {
            "filter[query]": self.query,
            "filter[from]": frm.isoformat(),
            "page[limit]": "100",
            # omit filter[to] to get up to now
        }
        total = 0
        url = self._api_url()
        next_page: Optional[str] = None
        while True:
            req_url = next_page or url
            resp = await client.get(req_url, params=params if next_page is None else None)
            resp.raise_for_status()
            data: Dict[str, Any] = resp.json()
            for item in data.get("data", []) or []:
                try:
                    attrs = (item.get("attributes") or {})
                    msg = attrs.get("message") or ""
                    if not msg:
                        continue
                    # Use os hint in source so downstream OS routing can work if desired
                    source = f"datadog:{self.os_hint}"
                    await safe_xadd(STREAM_NAME, {"source": source, "line": str(msg).strip()})
                    total += 1
                except Exception as exc:  # noqa: BLE001
                    LOG.info("datadog: failed to emit log err=%s", exc)
            links = data.get("links") or {}
            next_page = links.get("next")
            if not next_page:
                break
        # Advance since time
        self._since = now
        return total

    async def run(self) -> None:
        await wait_for_redis()
        if not self.api_key or not self.app_key:
            LOG.info("datadog: missing api_key/app_key; not starting")
            while not self._stop:
                await asyncio.sleep(60)
            return
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=30) as client:
            while not self._stop:
                try:
                    count = await self._poll_once(client)
                    LOG.info("datadog: fetched %d logs", count)
                except Exception as exc:  # noqa: BLE001
                    LOG.info("datadog poll failed err=%s", exc)
                await asyncio.sleep(self.poll_interval_sec)

    async def shutdown(self) -> None:
        self._stop = True


@register("datadog")
def _factory(cfg: dict):
    return Datadog(cfg)



