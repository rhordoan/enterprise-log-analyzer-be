import asyncio
import logging

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import get_settings


settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
LOG = logging.getLogger(__name__)

STREAM_NAME = "logs"


async def wait_for_redis() -> None:
    delay = 0.5
    while True:
        try:
            await redis.ping()
            return
        except RedisConnectionError:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5)


async def safe_xadd(stream: str, fields: dict, *, retry: int = 1) -> None:
    try:
        await redis.xadd(stream, fields, id="*")
    except RedisConnectionError:
        await wait_for_redis()
        if retry > 0:
            await safe_xadd(stream, fields, retry=retry - 1)








