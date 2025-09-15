import asyncio
from contextlib import suppress

import redis.asyncio as aioredis
from redis.exceptions import ResponseError
from fastapi import FastAPI

from app.core.config import get_settings

settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

STREAM_NAME = "logs"
GROUP_NAME = "log_consumers"
CONSUMER_NAME = "consumer_1"

async def consume_logs():
    """Consume new messages from Redis Stream and acknowledge them."""
    # create consumer group if not exists
    try:
        await redis.xgroup_create(STREAM_NAME, GROUP_NAME, id="$", mkstream=True)
    except ResponseError:
        pass

    while True:
        response = await redis.xreadgroup(
            GROUP_NAME,
            CONSUMER_NAME,
            {STREAM_NAME: ">"},
            count=10,
            block=1000,
        )
        if response:
            for _, messages in response:
                for msg_id, data in messages:
                    source = data.get("source")
                    line = data.get("line")
                    print(f"[{source}] {line}")
                    # acknowledge message
                    await redis.xack(STREAM_NAME, GROUP_NAME, msg_id)


def attach_consumer(app: FastAPI):
    @app.on_event("startup")
    async def startup_event():
        app.state.consumer_task = asyncio.create_task(consume_logs())

    @app.on_event("shutdown")
    async def shutdown_event():
        app.state.consumer_task.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.consumer_task
