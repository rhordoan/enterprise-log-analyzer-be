from __future__ import annotations

from typing import Callable, Dict, Any


_factories: Dict[str, Callable[[Dict[str, Any]], object]] = {}


def register(name: str):
    def deco(factory: Callable[[Dict[str, Any]], object]):
        _factories[name] = factory
        return factory
    return deco


def get_factory(name: str) -> Callable[[Dict[str, Any]], object]:
    if name not in _factories:
        raise KeyError(f"Unknown producer type: {name}")
    return _factories[name]





