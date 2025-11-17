from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from statistics import mean, pstdev
from typing import Any, Deque, Dict, Tuple

import redis.asyncio as aioredis

from app.core.config import get_settings


LOG = logging.getLogger(__name__)


class RollingStats:
    def __init__(self, maxlen: int = 60):
        self.values: Deque[float] = deque(maxlen=maxlen)

    def push(self, v: float) -> None:
        self.values.append(v)

    def zscore(self) -> float:
        if len(self.values) < 5:
            return 0.0
        m = mean(self.values)
        s = pstdev(self.values)
        if s == 0:
            return 0.0
        return (self.values[-1] - m) / s


class FailurePredictor:
    def __init__(self, window: int = 60, z_alert: float = 3.0) -> None:
        self.window = window
        self.z_alert = z_alert
        self.series: Dict[Tuple[str, str], RollingStats] = defaultdict(lambda: RollingStats(maxlen=window))

    def ingest(self, host: str, name: str, value: float) -> list[dict[str, Any]]:
        key = (host, name)
        stats = self.series[key]
        stats.push(value)
        z = stats.zscore()
        if abs(z) >= self.z_alert:
            return [{
                "host": host,
                "metric": name,
                "zscore": z,
                "value": value,
                "severity": "high" if abs(z) >= (self.z_alert + 1.0) else "medium",
            }]
        return []


async def run_failure_prediction() -> None:
    """Background task that reads normalized metrics and emits early warnings.

    This is intentionally decoupled: it does not persist or route incidents yet.
    Integrators can wire alerts to existing pipelines as needed.
    """
    settings = get_settings()
    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    predictor = FailurePredictor()

    stream = "metrics"
    group = "predictors"
    consumer = "predictor_1"

    # Create group if needed
    try:
        await redis.xgroup_create(stream, group, id="$", mkstream=True)
    except Exception:
        pass

    while True:
        try:
            rows = await redis.xreadgroup(group, consumer, {stream: ">"}, count=100, block=1000)
        except Exception as exc:
            LOG.info("predictor: xreadgroup err=%s", exc)
            await asyncio.sleep(1)
            continue
        if not rows:
            continue
        ack_ids: list[str] = []
        for _, messages in rows:
            for msg_id, fields in messages:
                try:
                    host = ""
                    try:
                        resource = json.loads(fields.get("resource") or "{}")
                        host = str(resource.get("host") or "")
                    except Exception:
                        host = ""
                    name = str(fields.get("name") or "")
                    value = float(fields.get("value") or 0)
                    if not host or not name:
                        ack_ids.append(msg_id)
                        continue
                    alerts = predictor.ingest(host, name, value)
                    # For now, write alerts to a debug Redis key; future: unify with incidents
                    if alerts:
                        key = f"predict:{host}:{name}"
                        try:
                            await redis.set(key, json.dumps({"last": alerts[-1]}), ex=3600)
                        except Exception:
                            pass
                    ack_ids.append(msg_id)
                except Exception:
                    ack_ids.append(msg_id)
        if ack_ids:
            try:
                await redis.xack(stream, group, *ack_ids)
            except Exception:
                pass



















