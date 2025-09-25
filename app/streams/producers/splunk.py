from __future__ import annotations

import asyncio
import json
import logging

import httpx

from app.streams.producers.base import ProducerPlugin
from app.streams.producers.registry import register
from app.streams.utils import STREAM_NAME, safe_xadd, wait_for_redis


LOG = logging.getLogger(__name__)


class Splunk(ProducerPlugin):
    name = "splunk"

    def __init__(self, cfg: dict):
        base = (cfg.get("base_url") or "").rstrip("/")
        self.url = f"{base}/services/search/jobs/export"
        self.params = {
            "search": f"search {cfg.get('search', '')}",
            "output_mode": "json",
        }
        if cfg.get("earliest"):
            self.params["earliest_time"] = cfg["earliest"]
        if cfg.get("latest"):
            self.params["latest_time"] = cfg["latest"]
        token = cfg.get("token") or ""
        self.headers = {"Authorization": f"Splunk {token}"}
        self.verify = bool(cfg.get("verify_ssl", True))
        self._stop = False

    async def run(self) -> None:
        await wait_for_redis()
        if not self.url or not self.headers.get("Authorization"):
            LOG.info("splunk: missing base_url/token; not starting")
            while not self._stop:
                await asyncio.sleep(60)
            return
        async with httpx.AsyncClient(verify=self.verify, timeout=None) as client:
            async with client.stream("GET", self.url, params=self.params, headers=self.headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if self._stop:
                        break
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        result = obj.get("result") or {}
                        raw = result.get("_raw") or ""
                        if raw:
                            await safe_xadd(STREAM_NAME, {"source": "splunk:unknown", "line": raw})
                    except Exception as exc:
                        LOG.info("splunk stream parse failed err=%s", exc)
                        await asyncio.sleep(0.1)

    async def shutdown(self) -> None:
        self._stop = True


@register("splunk")
def _factory(cfg: dict):
    return Splunk(cfg)



