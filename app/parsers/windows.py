from __future__ import annotations

import re
from typing import Dict, Optional


# Windows CBS log format sample:
# 2016-09-28 04:30:30, Info  CBS    Message
WINDOWS_REGEX = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2}),\s+"
    r"(?P<level>\w+)\s+"
    r"(?P<component>\S+)\s+"
    r"(?P<content>.*)$"
)


def parse_windows_line(line_id: int, line: str) -> Optional[Dict[str, str]]:
    match = WINDOWS_REGEX.match(line.rstrip("\n"))
    if not match:
        return None
    data = match.groupdict()
    return {
        "lineId": str(line_id),
        "date": data.get("date", ""),
        "time": data.get("time", ""),
        "level": data.get("level", ""),
        "component": data.get("component", ""),
        "content": data.get("content", ""),
    }


