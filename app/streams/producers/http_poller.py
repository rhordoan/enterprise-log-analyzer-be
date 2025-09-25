from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from app.streams.producers.base import ProducerPlugin
from app.streams.producers.registry import register
from app.streams.utils import STREAM_NAME, safe_xadd, wait_for_redis


LOG = logging.getLogger(__name__)


class DCIMHttpPoller(ProducerPlugin):
    name = "dcim_http"

    def __init__(self, config: dict[str, Any]):
        # Example config:
        # {
        #   "endpoints": [
        #       {"url": "https://dcim/api/sensors", "headers": {"Authorization": "Bearer ..."}},
        #       {"url": "https://dcim/api/alarms"}
        #   ],
        #   "poll_interval_sec": 30,
        # }
        self.endpoints: list[dict[str, Any]] = list(config.get("endpoints") or [])
        self.interval: float = float(config.get("poll_interval_sec", 30))
        self.verify_ssl: bool = bool(config.get("verify_ssl", True))
        self._stop = False
        self._source_id: int | None = int(config.get("_source_id")) if config.get("_source_id") is not None else None

    async def _poll_endpoint(self, ep: dict[str, Any]) -> None:
        url: str = str(ep.get("url") or "")
        method: str = str(ep.get("method") or "GET").upper()
        headers: dict[str, str] = dict(ep.get("headers") or {})
        params: dict[str, Any] = dict(ep.get("params") or {})
        data: Any = ep.get("data")
        if not url:
            return
        parsed = urlparse(url)
        src = f"dcim_http:{parsed.hostname or 'unknown'}"

        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=None) as client:
            while not self._stop:
                try:
                    resp = await client.request(method, url, headers=headers, params=params, json=data)
                    resp.raise_for_status()
                    text = resp.text
                    # Try to parse JSON; fallback to text
                    try:
                        body = resp.json()
                    except Exception:  # noqa: BLE001
                        body = text
                    payload = {
                        "url": url,
                        "status": resp.status_code,
                        "body": body,
                    }
                    await safe_xadd(
                        STREAM_NAME,
                        {
                            "source": src,
                            "line": json.dumps(payload, ensure_ascii=False),
                            **({"source_id": str(self._source_id)} if self._source_id is not None else {}),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    LOG.info("dcim_http: request error url=%s err=%s", url, exc)
                await asyncio.sleep(self.interval)

    async def run(self) -> None:
        await wait_for_redis()
        if not self.endpoints:
            LOG.info("dcim_http: no endpoints configured; idle")
            while not self._stop:
                await asyncio.sleep(60)
            return
        tasks = [asyncio.create_task(self._poll_endpoint(ep)) for ep in self.endpoints]
        await asyncio.gather(*tasks)

    async def shutdown(self) -> None:
        self._stop = True


@register("dcim_http")
def _factory(cfg: dict):
    return DCIMHttpPoller(cfg)


