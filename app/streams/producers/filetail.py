from __future__ import annotations

import asyncio
import os
from pathlib import Path
import logging

import aiofiles

from app.streams.producers.base import ProducerPlugin
from app.streams.producers.registry import register
from app.streams.utils import STREAM_NAME, safe_xadd, wait_for_redis


LOG = logging.getLogger(__name__)


class FileTail(ProducerPlugin):
    name = "filetail"

    def __init__(self, config: dict):
        paths = config.get("paths") or ["data/Linux.log", "data/Mac.log"]
        self.paths = [Path(p) for p in paths]
        # Text decoding configuration; Windows defaults can cause decode errors on arbitrary logs
        self.encoding = config.get("encoding") or "utf-8"
        self.errors = config.get("errors") or "replace"
        self._stop = False
        LOG.info(
            "filetail: configured paths=%s encoding=%s errors=%s",
            ", ".join(str(p) for p in self.paths),
            self.encoding,
            self.errors,
        )

    async def _tail(self, path: Path) -> None:
        source = path.name
        backoff = 1.0
        while not self._stop:
            # Wait for file to appear if it's not there yet
            while not self._stop and not path.exists():
                await asyncio.sleep(1.0)
            if self._stop:
                return
            try:
                LOG.info("filetail: opening %s", path)
                async with aiofiles.open(path, mode="r", encoding=self.encoding, errors=self.errors) as f:
                    # Read existing content once
                    await f.seek(0)
                    while not self._stop:
                        line = await f.readline()
                        if not line:
                            break
                        await safe_xadd(STREAM_NAME, {"source": source, "line": line.strip()})
                    # Seek to EOF and follow
                    await f.seek(0, os.SEEK_END)
                    while not self._stop:
                        line = await f.readline()
                        if not line:
                            await asyncio.sleep(0.5)
                            continue
                        await safe_xadd(STREAM_NAME, {"source": source, "line": line.strip()})
                # successful pass; reset backoff
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001
                LOG.exception("filetail: error while tailing %s: %s", path, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def run(self) -> None:
        await wait_for_redis()
        # Alert on any missing files at startup
        missing = [str(p) for p in self.paths if not p.exists()]
        if missing:
            LOG.error("filetail: missing files at startup: %s", ", ".join(missing))
        # Always start tasks for configured paths; each task waits for file to appear
        LOG.info("filetail: starting tails for %d paths", len(self.paths))
        tasks = [asyncio.create_task(self._tail(p)) for p in self.paths]
        await asyncio.gather(*tasks)

    async def shutdown(self) -> None:
        self._stop = True


@register("filetail")
def _factory(cfg: dict):
    return FileTail(cfg)



