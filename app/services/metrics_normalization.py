from __future__ import annotations

from time import time
from typing import Any, Callable, Dict, List, TypedDict


class MetricPoint(TypedDict, total=False):
    name: str
    type: str              # "gauge" | "sum" | "histogram"
    value: Any             # number or {sum,count,buckets}
    unit: str | None
    time_unix_nano: int
    resource: Dict[str, Any]
    attributes: Dict[str, Any]


Normalizer = Callable[[str, Dict[str, Any], Dict[str, Any]], List[MetricPoint]]
_registry: Dict[str, Normalizer] = {}


def register_normalizer(kind: str):
    def deco(fn: Normalizer):
        _registry[kind] = fn
        return fn
    return deco


def normalize(kind: str, payload: Dict[str, Any], config: Dict[str, Any]) -> List[MetricPoint]:
    fn = _registry.get(kind)
    if not fn:
        return []
    return fn(kind, payload, config)


def now_nano() -> int:
    return int(time() * 1e9)



