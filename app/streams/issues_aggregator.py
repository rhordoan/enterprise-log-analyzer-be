import asyncio
import logging
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.parsers.linux import parse_linux_line
from app.parsers.macos import parse_macos_line
from app.parsers.templating import render_templated_line


settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
LOG = logging.getLogger(__name__)


def _os_from_source(source: str | None) -> str:
    if not source:
        return "unknown"
    s = source.lower()
    if "linux.log" in s:
        return "linux"
    if "mac.log" in s:
        return "macos"
    if "windows" in s:
        return "windows"
    return "unknown"


def _parse_and_template(os_name: str, line: str) -> Tuple[str, Dict[str, str]]:
    parsed: Dict[str, str] | None = None
    if os_name == "linux":
        parsed = parse_linux_line(0, line) or None
    elif os_name == "macos":
        parsed = parse_macos_line(0, line) or None
    if not parsed:
        templated = render_templated_line(component="unknown", pid=None, content=line)
        return templated, {"content": line, "component": "unknown"}
    templated = render_templated_line(
        component=parsed.get("component", ""),
        pid=parsed.get("PID"),
        content=parsed.get("content", ""),
    )
    return templated, parsed


def _issue_key(os_name: str, parsed: Dict[str, str]) -> str:
    component = parsed.get("component", "unknown").lower().strip()
    pid = parsed.get("PID", "").strip()
    return f"{os_name}|{component}|{pid or 'nopid'}"


@dataclass
class Issue:
    os: str
    key: str
    created_at: float
    last_seen_at: float
    logs: List[Dict[str, Any]] = field(default_factory=list)

    def add_log(self, raw: str, templated: str, parsed: Dict[str, str]) -> None:
        now = time.time()
        self.logs.append({
            "raw": raw,
            "templated": templated,
            "parsed": parsed,
            "ts": now,
        })
        self.last_seen_at = now

    def top_logs(self, limit: int) -> List[Dict[str, Any]]:
        # naive heuristic: keep order, cap length
        return self.logs[:limit]


_issues: Dict[str, Issue] = {}


async def _close_and_publish(issue: Issue) -> None:
    # Serialize logs as JSON; Redis stream field values must be strings
    logs_list = [
        {
            "templated": log["templated"],
            "raw": log["raw"],
            "component": log["parsed"].get("component", ""),
            "pid": log["parsed"].get("PID", ""),
            "time": log.get("ts", 0),
        }
        for log in issue.top_logs(settings.ISSUE_MAX_LOGS_FOR_LLM)
    ]
    payload = {
        "os": issue.os,
        "issue_key": issue.key,
        # send compact representation: concatenate templated as a rough summary
        "templated_summary": " \n".join([log["templated"] for log in issue.top_logs(settings.ISSUE_MAX_LOGS_FOR_LLM)]),
        "logs": __import__("json").dumps(logs_list),
    }
    await redis.xadd(settings.ISSUES_CANDIDATES_STREAM, payload)
    LOG.info("published issue os=%s key=%s logs=%d", issue.os, issue.key, len(issue.logs))


async def run_issues_aggregator() -> None:
    """Consume raw logs from 'logs' stream, group them into issues, publish issues when idle."""
    stream = "logs"
    group = "issues_aggregator"
    consumer = "aggregator_1"
    # Create group if it doesn't exist
    try:
        await redis.xgroup_create(stream, group, id="$", mkstream=True)
        LOG.info("group created stream=%s group=%s", stream, group)
    except Exception as exc:
        LOG.info("group exists stream=%s group=%s info=%s", stream, group, exc)

    inactivity = float(settings.ISSUE_INACTIVITY_SEC)

    LOG.info("starting issues aggregator stream=%s group=%s consumer=%s", stream, group, consumer)
    while True:
        # read new messages
        try:
            response = await redis.xreadgroup(group, consumer, {stream: ">"}, count=100, block=1000)
        except Exception as exc:
            LOG.info("xreadgroup failed stream=%s group=%s consumer=%s err=%s", stream, group, consumer, exc)
            await asyncio.sleep(1)
            continue
        now = time.time()
        if response:
            processed = 0
            for _, messages in response:
                for msg_id, data in messages:
                    processed += 1
                    source = data.get("source")
                    raw = data.get("line") or ""
                    os_name = _os_from_source(source)
                    templated, parsed = _parse_and_template(os_name, raw)
                    key = _issue_key(os_name, parsed)
                    issue = _issues.get(key)
                    if issue is None:
                        issue = Issue(os=os_name, key=key, created_at=now, last_seen_at=now)
                        _issues[key] = issue
                    issue.add_log(raw=raw, templated=templated, parsed=parsed)
                    # We do not ack here; base consumer owns acking, we only observe this stream via separate group
            LOG.debug("aggregated messages=%d open_issues=%d", processed, len(_issues))
        # periodically close idle issues
        to_close: List[str] = []
        for key, issue in _issues.items():
            if now - issue.last_seen_at >= inactivity:
                await _close_and_publish(issue)
                to_close.append(key)
        for key in to_close:
            _issues.pop(key, None)


def attach_issues_aggregator(app):
    async def _run_forever():
        backoff = 1.0
        while True:
            try:
                await run_issues_aggregator()
            except Exception as exc:
                LOG.info("issues aggregator crashed err=%s; restarting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)

    @app.on_event("startup")
    async def startup_event():
        LOG.info("attaching issues aggregator")
        app.state.issues_task = asyncio.create_task(_run_forever())

    @app.on_event("shutdown")
    async def shutdown_event():
        LOG.info("stopping issues aggregator")
        app.state.issues_task.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.issues_task


