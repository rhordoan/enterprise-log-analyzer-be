import asyncio
import json
import os

import redis.asyncio as aioredis


async def main() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    redis = aioredis.from_url(redis_url, decode_responses=True)
    # Minimal SCOM-like alert payload
    payload = {
        "type": "alert",
        "Severity": "Critical",
        "Name": "SQL Server service stopped",
        "MonitoringObjectDisplayName": "DB01",
    }
    await redis.xadd(
        "logs",
        {
            "source": "scom:mock",
            "line": json.dumps(payload),
        },
    )
    print("Queued synthetic SCOM alert to 'logs' stream")


if __name__ == "__main__":
    asyncio.run(main())




