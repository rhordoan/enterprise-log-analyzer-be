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


class SquaredUpProducer(ProducerPlugin):
    name = "squaredup"

    def __init__(self, config: Dict[str, Any]):
        # Config:
        # {
        #   "base_url": "https://squaredup",
        #   "api_key": "xxx",
        #   "header_name": "X-Api-Key",
        #   "verify_ssl": true,
        #   "poll_seconds": 30,
        #   "health_path": "/api/health",
        #   "alerts_path": "/api/alerts",
        #   "deps_path": "/api/dependencies"
        # }
        self.base_url: str = str(config.get("base_url") or "").rstrip("/")
        self.api_key: str = str(config.get("api_key") or "")
        self.header_name: str = str(config.get("header_name") or "X-Api-Key")
        self.verify_ssl: bool = bool(config.get("verify_ssl", True))
        self.poll_seconds: float = float(config.get("poll_seconds", 30))
        self.health_path: str = str(config.get("health_path") or "/api/health")
        self.alerts_path: str = str(config.get("alerts_path") or "/api/alerts")
        self.deps_path: str = str(config.get("deps_path") or "/api/dependencies")
        self._stop = False
        parsed = urlparse(self.base_url)
        self._src_prefix = f"squaredup:{parsed.hostname or 'unknown'}"

    async def _fetch(self, path: str) -> Optional[Any]:
        if not self.base_url or not self.api_key:
            return None
        url = f"{self.base_url}{path}"
        headers = {self.header_name: self.api_key}
        try:
            async with httpx.AsyncClient(verify=self.verify_ssl, timeout=None) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                try:
                    return resp.json()
                except Exception:
                    return resp.text
        except Exception as exc:  # noqa: BLE001
            LOG.info("squaredup: request failed path=%s err=%s", path, exc)
            return None

    async def run(self) -> None:
        await wait_for_redis()
        if not (self.base_url and self.api_key):
            LOG.info("squaredup: missing configuration; idle")
            while not self._stop:
                await asyncio.sleep(60)
            return
        backoff = 1.0
        while not self._stop:
            try:
                for path, typ in (
                    (self.health_path, "health"),
                    (self.alerts_path, "alert"),
                    (self.deps_path, "dependency"),
                ):
                    body = await self._fetch(path)
                    if body is None:
                        continue
                    if isinstance(body, list):
                        items = body
                    elif isinstance(body, dict) and "items" in body:
                        items = body.get("items") or []
                    else:
                        items = [body]
                    for it in items:
                        try:
                            await safe_xadd(
                                STREAM_NAME,
                                {
                                    "source": self._src_prefix,
                                    "line": json.dumps({"type": typ, **(it if isinstance(it, dict) else {"value": it})}, ensure_ascii=False),
                                },
                            )
                        except Exception:
                            pass
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001
                LOG.info("squaredup: poll failed err=%s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            await asyncio.sleep(self.poll_seconds)

    async def shutdown(self) -> None:
        self._stop = True


@register("squaredup")
def _factory(cfg: dict):
    return SquaredUpProducer(cfg)





