from __future__ import annotations

import re
from typing import Optional


# Patterns ordered from most specific to most general to avoid over-masking
MAC_ADDRESS = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
IPV4_ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_ADDRESS = re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,}[A-Fa-f0-9]{1,4}\b")
UUID_PATTERN = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
HEX_LITERAL = re.compile(r"\b0x[0-9A-Fa-f]+\b")
VERSION_PATTERN = re.compile(r"\b\d+(?:\.\d+){1,3}\b")
HASH_NUMBER = re.compile(r"#\d+")
NUMBER = re.compile(r"(?<!\w)[-+]?\d+(?:\.\d+)?(?!\w)")


def template_content(message: str) -> str:
    """Return a templated version of a log message body by masking variable tokens.

    The function attempts to preserve structure while replacing volatile values with `<*>`.
    """
    templated = message
    templated = MAC_ADDRESS.sub("<*>", templated)
    templated = IPV4_ADDRESS.sub("<*>", templated)
    templated = IPV6_ADDRESS.sub("<*>", templated)
    templated = UUID_PATTERN.sub("<*>", templated)
    templated = HEX_LITERAL.sub("<*>", templated)
    templated = VERSION_PATTERN.sub("<*>", templated)
    templated = HASH_NUMBER.sub("#<*>", templated)
    templated = NUMBER.sub("<*>", templated)
    # collapse excessive whitespace that may appear after substitutions
    templated = re.sub(r"\s+", " ", templated).strip()
    return templated


def render_templated_line(component: str, pid: Optional[str], content: str) -> str:
    """Build a templated full line like `component[PID]: <templated content>`.

    If `pid` is falsy, the bracketed PID segment is omitted.
    """
    templated_body = template_content(content)
    pid_part = f"[{pid}]" if pid else ""
    separator = ": " if templated_body else ""
    return f"{component}{pid_part}{separator}{templated_body}"


