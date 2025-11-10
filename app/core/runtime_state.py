from __future__ import annotations

# Global runtime state flags

is_shutting_down: bool = False


def set_shutting_down(value: bool) -> None:
    global is_shutting_down
    is_shutting_down = bool(value)


