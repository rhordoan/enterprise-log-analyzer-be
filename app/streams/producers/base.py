from __future__ import annotations

from typing import Any, Protocol


class ProducerPlugin(Protocol):
    name: str

    def __init__(self, config: dict[str, Any]):
        ...

    async def run(self) -> None:
        ...

    async def shutdown(self) -> None:
        ...





