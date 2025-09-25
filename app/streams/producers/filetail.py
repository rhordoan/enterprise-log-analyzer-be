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
        self._stop = False
        LOG.info("filetail: configured paths=%s", ", ".join(str(p) for p in self.paths))

    async def _tail(self, path: Path) -> None:
        source = path.name
        # Wait for file to appear if it's not there yet
        while not self._stop and not path.exists():
            LOG.info("filetail: waiting for %s to appear", path)
            await asyncio.sleep(1.0)
        if self._stop:
            return
        LOG.info("filetail: opening %s", path)
        async with aiofiles.open(path, mode="r") as f:
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

    async def run(self) -> None:
        await wait_for_redis()
        # Always start tasks for configured paths; each task waits for file to appear
        LOG.info("filetail: starting tails for %d paths", len(self.paths))
        tasks = [asyncio.create_task(self._tail(p)) for p in self.paths]
        await asyncio.gather(*tasks)

    async def shutdown(self) -> None:
        self._stop = True


@register("filetail")
def _factory(cfg: dict):
    return FileTail(cfg)



