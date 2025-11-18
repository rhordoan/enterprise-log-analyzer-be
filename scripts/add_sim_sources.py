from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val else default


def _request(base_url: str, method: str, path: str, body: Dict[str, Any] | None = None) -> Any:
    url = base_url.rstrip("/") + path
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted local admin script
        if resp.status >= 400:
            raise RuntimeError(f"{method} {url} failed status={resp.status}")
        if resp.length == 0:
            return None
        content_type = resp.headers.get("Content-Type", "")
        raw = resp.read()
        if "application/json" in content_type:
            return json.loads(raw.decode("utf-8"))
        return raw.decode("utf-8")


def _get_sources(base_url: str) -> List[Dict[str, Any]]:
    try:
        res = _request(base_url, "GET", "/api/v1/sources/")
        if isinstance(res, list):
            return res
        return []
    except Exception:
        return []


def _exists(sources: List[Dict[str, Any]], name: str) -> bool:
    for s in sources:
        if str(s.get("name") or "").strip().lower() == name.strip().lower():
            return True
    return False


def create_sources(api_base: str, sim_base: str) -> None:
    sources = _get_sources(api_base)

    payloads = [
        {
            "name": "sim-scom",
            "type": "scom",
            "enabled": True,
            "config": {
                "base_url": sim_base,
                "domain": "CONTOSO",
                "username": "svc_scom",
                "password": "secret",
                "verify_ssl": False,
                "poll_seconds": 15,
                "alerts_path": "/OperationsManager/data/alert",
                "perf_path": "/OperationsManager/data/performance",
                "events_path": "/OperationsManager/data/event",
            },
        },
        {
            "name": "sim-squaredup",
            "type": "squaredup",
            "enabled": True,
            "config": {
                "base_url": sim_base,
                "api_key": "demo-key",
                "header_name": "X-Api-Key",
                "verify_ssl": False,
                "poll_seconds": 15,
                "health_path": "/api/health",
                "alerts_path": "/api/alerts",
                "deps_path": "/api/dependencies",
            },
        },
        {
            "name": "sim-catalyst",
            "type": "catalyst",
            "enabled": True,
            "config": {
                "base_url": sim_base,
                "username": "admin",
                "password": "Cisco123",
                "verify_ssl": False,
                "poll_seconds": 20,
                "auth_path": "/dna/system/api/v1/auth/token",
                "health_paths": {
                    "network": "/dna/intent/api/v1/network-health",
                    "client": "/dna/intent/api/v1/client-health",
                    "device": "/dna/intent/api/v1/device-health",
                },
                "events_path": "/dna/intent/api/v1/events",
            },
        },
        {
            "name": "sim-thousandeyes",
            "type": "thousandeyes",
            "enabled": True,
            "config": {
                "base_url": sim_base,
                "bearer_token": "demo-token",
                "verify_ssl": False,
                "poll_interval_sec": 15,
                "alerts_path": "/v6/alerts.json",
                "tests_path": "/v6/tests.json",
                "window": "5m",
            },
        },
    ]

    created = 0
    skipped = 0
    for p in payloads:
        name = str(p.get("name"))
        if _exists(sources, name):
            print(f"skip: already exists -> {name}")
            skipped += 1
            continue
        try:
            _request(api_base, "POST", "/api/v1/sources/", body=p)
            print(f"created: {name}")
            created += 1
        except urllib.error.HTTPError as e:
            print(f"error: {name} -> HTTP {e.code} {e.reason}")
        except Exception as e:
            print(f"error: {name} -> {e}")

    print(f"done. created={created} skipped={skipped}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create simulator data sources in the backend app")
    parser.add_argument("--api-base", default=_env("API_BASE_URL", "http://localhost:8000"), help="Backend API base URL")
    parser.add_argument("--sim-base", default=_env("SIM_BASE_URL", "http://localhost:8085"), help="Simulator base URL")
    args = parser.parse_args()
    create_sources(args.api_base, args.sim_base)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        sys.exit(1)






