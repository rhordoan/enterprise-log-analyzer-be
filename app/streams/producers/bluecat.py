from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from app.streams.producers.base import ProducerPlugin
from app.streams.producers.registry import register
from app.streams.utils import STREAM_NAME, safe_xadd, wait_for_redis


LOG = logging.getLogger(__name__)


class BlueCatProducer(ProducerPlugin):
    name = "bluecat"

    def __init__(self, config: Dict[str, Any]):
        # Config:
        # { "base_url": "https://bluecat", "api_token": "xxx", "verify_ssl": true, "poll_seconds": 30, "events_path": "/Services/REST/v1/events" }
        self.base_url: str = str(config.get("base_url") or "").rstrip("/")
        self.api_token: str = str(config.get("api_token") or "")
        self.verify_ssl: bool = bool(config.get("verify_ssl", True))
        self.poll_seconds: float = float(config.get("poll_seconds", 30))
        self.events_path: str = str(config.get("events_path") or "/Services/REST/v1/events")
        self._stop = False
        parsed = urlparse(self.base_url)
        self._src_prefix = f"bluecat:{parsed.hostname or 'unknown'}"

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    async def run(self) -> None:
        await wait_for_redis()
        if not (self.base_url and self.events_path):
            LOG.info("bluecat: missing configuration; idle")
            while not self._stop:
                await asyncio.sleep(60)
            return
        backoff = 1.0
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=None) as client:
            while not self._stop:
                try:
                    url = f"{self.base_url}{self.events_path}"
                    resp = await client.get(url, headers=self._headers())
                    resp.raise_for_status()
                    try:
                        body = resp.json()
                    except Exception:
                        body = []
                    items = []
                    if isinstance(body, dict) and "items" in body:
                        items = body.get("items") or []
                    elif isinstance(body, list):
                        items = body
                    for it in items:
                        try:
                            await safe_xadd(STREAM_NAME, {"source": self._src_prefix, "line": json.dumps({"type": "event", **(it if isinstance(it, dict) else {"value": it})}, ensure_ascii=False)})
                        except Exception:
                            pass
                    backoff = 1.0
                except Exception as exc:  # noqa: BLE001
                    LOG.info("bluecat: poll failed err=%s", exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                await asyncio.sleep(self.poll_seconds)

    async def shutdown(self) -> None:
        self._stop = True


@register("bluecat")
def _factory(cfg: dict):
    return BlueCatProducer(cfg)





