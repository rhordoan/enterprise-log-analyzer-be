from __future__ import annotations

import re
from typing import Dict, Optional


# Typical Linux syslog format:
# Jun 14 15:16:01 host component[PID]: level? message
LINUX_REGEX = re.compile(
    r"^(?P<month>\w{3})\s+(?P<date>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+"
    r"(?P<component>[^\[]+?)\[(?P<PID>\d+)\]:\s+"
    r"(?P<content>.*)$"
)

# Best-effort extract a log level token from content when present
LEVEL_REGEX = re.compile(r"\b(INFO|DEBUG|WARN|WARNING|ERROR|CRITICAL|ALERT)\b", re.IGNORECASE)


def parse_linux_line(line_id: int, line: str) -> Optional[Dict[str, str]]:
    match = LINUX_REGEX.match(line.rstrip("\n"))
    if not match:
        return None
    data = match.groupdict()
    content = data.get("content", "")
    level_match = LEVEL_REGEX.search(content)
    level = level_match.group(0).upper() if level_match else ""
    return {
        "lineId": str(line_id),
        "month": data.get("month", ""),
        "date": data.get("date", ""),
        "time": data.get("time", ""),
        "level": level,
        "component": data.get("component", "").strip(),
        "PID": data.get("PID", ""),
        "content": content,
    }


