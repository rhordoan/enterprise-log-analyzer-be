import asyncio
import os
import logging
from pathlib import Path

import aiofiles
from fastapi import FastAPI
import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
import threading

from app.core.config import get_settings

settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

STREAM_NAME = "logs"
LOG = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


async def _tail_file(path: Path):
    """Push existing lines from `path` and then follow new lines, sending each to Redis."""
    source = path.name
    async with aiofiles.open(path, mode="r") as f:
        # push existing lines
        await f.seek(0)
        while True:
            line = await f.readline()
            if not line:
                break
            await _safe_xadd(STREAM_NAME, {"source": source, "line": line.strip()})
            LOG.debug("pushed existing line from %s", source)

        # now follow new lines
        await f.seek(0, os.SEEK_END)
        while True:
            line = await f.readline()
            if not line:
                await asyncio.sleep(0.5)
                continue
            await _safe_xadd(STREAM_NAME, {"source": source, "line": line.strip()})
            LOG.info("pushed new line from %s", source)


async def _wait_for_redis() -> None:
    """Block until Redis is reachable (keeps retrying with backoff)."""
    delay = 0.5
    while True:
        try:
            await redis.ping()
            LOG.info("Connected to Redis at %s", settings.REDIS_URL)
            return
        except RedisConnectionError as exc:
            LOG.info("Redis not available (%s). Retrying in %.1fs...", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5)


async def _safe_xadd(stream: str, fields: dict, *, retry: int = 1) -> None:
    """Perform xadd with one automatic reconnect/retry on connection errors."""
    try:
        await redis.xadd(stream, fields, id="*")
    except RedisConnectionError as exc:
        LOG.info("xadd failed (%s). Waiting for Redis and retrying...", exc)
        await _wait_for_redis()
        if retry > 0:
            await _safe_xadd(stream, fields, retry=retry - 1)
        else:
            LOG.info("xadd retry exhausted stream=%s fields=%s", stream, fields)


async def produce_logs():
    """Tail specific log files under data/ and push lines to Redis Stream concurrently.

    The application expects logs to be provided in `data/Linux.log` and
    `data/Mac.log`. Wait for Redis to be available, log startup info, and
    then tail the existing files concurrently.
    """
    data_dir = Path("data")
    LOG.info("Starting producer; REDIS_URL=%s data_dir=%s", settings.REDIS_URL, data_dir)

    # Ensure Redis is reachable before starting to tail files
    await _wait_for_redis()
    LOG.info("producer ready; Redis reachable at %s, starting to collect files", settings.REDIS_URL)

    tasks = []
    expected_files = ["Linux.log", "Mac.log"]
    found_paths = []

    for name in expected_files:
        path = data_dir / name
        if path.exists():
            found_paths.append(path)
            tasks.append(asyncio.create_task(_tail_file(path)))
        else:
            LOG.info("Expected log file not found: %s", path)

    if not tasks:
        LOG.info("No log files found to tail under %s", data_dir)
        while True:
            await asyncio.sleep(3600)

    LOG.info("Tailing %d files: %s", len(found_paths), ", ".join([p.name for p in found_paths]))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(produce_logs())


def attach_producer(app: FastAPI):
    async def _run_forever():
        backoff = 1.0
        while True:
            try:
                await produce_logs()
            except Exception as exc:
                LOG.info("producer crashed err=%s; restarting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    @app.on_event("startup")
    async def startup_event():
        LOG.info("starting producer in dedicated thread")
        loop = asyncio.new_event_loop()

        def _runner():
            asyncio.set_event_loop(loop)
            loop.create_task(_run_forever())
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="producer-thread", daemon=True)
        thread.start()
        app.state.producer_loop = loop
        app.state.producer_thread = thread

    @app.on_event("shutdown")
    async def shutdown_event():
        LOG.info("stopping producer thread")
        loop = getattr(app.state, "producer_loop", None)
        thread = getattr(app.state, "producer_thread", None)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
