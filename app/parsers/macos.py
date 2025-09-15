from __future__ import annotations

import re
from typing import Dict, Optional


# Example macOS log format:
# Jul  1 09:00:55 host component[PID]: message
MACOS_REGEX = re.compile(
    r"^(?P<month>\w{3})\s+(?P<date>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<user>\S+)\s+"
    r"(?P<component>[^\[]+?)\[(?P<PID>\d+)\]:\s+"
    r"(?P<content>.*)$"
)


def parse_macos_line(line_id: int, line: str) -> Optional[Dict[str, str]]:
    match = MACOS_REGEX.match(line.rstrip("\n"))
    if not match:
        return None
    data = match.groupdict()
    # Some messages include an address-like token, try to find IPv4/IPv6
    address_match = re.search(r"((?:\d{1,3}\.){3}\d{1,3})|([A-Fa-f0-9:]{2,})", data.get("content", ""))
    address = address_match.group(0) if address_match else ""
    return {
        "lineId": str(line_id),
        "month": data.get("month", ""),
        "date": data.get("date", ""),
        "time": data.get("time", ""),
        "user": data.get("user", ""),
        "component": data.get("component", "").strip(),
        "PID": data.get("PID", ""),
        "address": address,
        "content": data.get("content", ""),
    }


