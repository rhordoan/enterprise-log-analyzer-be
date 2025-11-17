# Mock Producers API (SCOM, SquaredUp, Cisco Catalyst Center)

This document describes a lightweight mock HTTP API you can run locally to synthesize realistic events for the following producers:

- SCOM (System Center Operations Manager) – REST (alerts, performance, events)
- SquaredUp – API key (health, alerts, dependencies)
- Cisco Catalyst Center (DNAC) – token auth (network/client/device health, events)

The mock API is purpose-built to drive the corresponding producers you already have, so you can demonstrate cross-tool correlation without live upstream systems.

## Overview

- Tech: Python + FastAPI (single service)
- Data: Generated on each request with consistent shapes and reasonable variability
- Auth:
  - SCOM: POST /OperationsManager/authenticate (Base64 body), then cookie + CSRF header (transparent in mock)
  - SquaredUp: X-Api-Key
  - Catalyst: X-Auth-Token acquired from POST /dna/system/api/v1/auth/token

## 1) Quickstart

Create a virtual environment and install dependencies:

```bash
python -m venv .venv && . .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install fastapi uvicorn faker python-dateutil
```

Save the following as `mock_api.py` and run:

```bash
uvicorn mock_api:app --host 0.0.0.0 --port 8085 --reload
```

Then point your data sources to `http://localhost:8085` per the config examples below.

## 2) Mock API Implementation (single file)

```python
# mock_api.py
from __future__ import annotations

import base64
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, Request, Response
from pydantic import BaseModel
from faker import Faker

fake = Faker()
app = FastAPI(title="Mock Producers API")

def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

def _rand_ip() -> str:
    return ".".join(str(random.randint(1, 254)) for _ in range(4))

# ---------------------------------------------
# SCOM (System Center Operations Manager)
# ---------------------------------------------
class CriteriaBody(BaseModel):
    __root__: str = ""

@app.post("/OperationsManager/authenticate")
async def scom_authenticate(body: str):
    # Body is base64-encoded "(Network):DOMAIN\\username:password"
    try:
        decoded = base64.b64decode(body.strip('"')).decode("utf-8")  # body arrives as JSON string
    except Exception:
        decoded = "(Network):mock\\user:pass"
    # Return a minimal OK; CSRF is initialized when UI root is fetched
    return {"status": "ok", "user": decoded}

@app.get("/OperationsManager")
async def scom_init_csrf(response: Response):
    # Attach a fake CSRF token header the client will reuse
    response.headers["X-CSRF-Token"] = fake.uuid4()
    return {"ok": True}

@app.post("/OperationsManager/data/alert")
async def scom_alerts(criteria: CriteriaBody):
    severities = ["Information", "Warning", "Error"]
    items = []
    for _ in range(random.randint(4, 10)):
        items.append({
            "Id": fake.uuid4(),
            "Name": fake.sentence(nb_words=3),
            "Severity": random.choice(severities),
            "Priority": random.choice(["Low", "Medium", "High"]),
            "MonitoringObjectDisplayName": fake.hostname(),
            "LastModified": _now_iso(),
        })
    return {"items": items}

@app.post("/OperationsManager/data/performance")
async def scom_performance(criteria: CriteriaBody):
    counters = [
        ("Processor", "Processor Time"),
        ("Memory", "Available MBytes"),
        ("LogicalDisk", "Avg. Disk sec/Read"),
    ]
    items = []
    for _ in range(random.randint(4, 10)):
        obj, ctr = random.choice(counters)
        items.append({
            "ObjectName": obj,
            "CounterName": ctr,
            "InstanceName": random.choice(["_Total", "0", "C:", "sda"]),
            "Value": round(random.uniform(0, 100), 3),
            "ComputerName": fake.hostname(),
            "Timestamp": _now_iso(),
        })
    return {"items": items}

@app.post("/OperationsManager/data/event")
async def scom_events(criteria: CriteriaBody):
    levels = ["Information", "Warning", "Error"]
    items = []
    for _ in range(random.randint(4, 10)):
        items.append({
            "LevelDisplayName": random.choice(levels),
            "ComputerName": fake.hostname(),
            "Channel": "Application",
            "Message": fake.sentence(nb_words=8),
            "TimeGenerated": _now_iso(),
        })
    return {"items": items}

# ---------------------------------------------
# SquaredUp (API Key)
# ---------------------------------------------
def _require_api_key(x_api_key: Optional[str]):
    if not x_api_key:
        return {"error": "missing_api_key"}

@app.get("/api/health")
async def squaredup_health(x_api_key: Optional[str] = Header(None, convert_underscores=False)):
    _require_api_key(x_api_key)
    # Return a few items representing application/infra health
    states = ["ok", "degraded", "critical"]
    items = [{
        "name": f"service-{i}",
        "state": random.choice(states),
        "updated": _now_iso(),
    } for i in range(3)]
    return {"items": items}

@app.get("/api/alerts")
async def squaredup_alerts(x_api_key: Optional[str] = Header(None, convert_underscores=False)):
    _require_api_key(x_api_key)
    severities = ["info", "warning", "critical"]
    items = [{
        "id": fake.uuid4(),
        "title": fake.sentence(nb_words=4),
        "severity": random.choice(severities),
        "created": _now_iso(),
    } for _ in range(random.randint(3, 8))]
    return {"items": items}

@app.get("/api/dependencies")
async def squaredup_dependencies(x_api_key: Optional[str] = Header(None, convert_underscores=False)):
    _require_api_key(x_api_key)
    # Simple edges
    edges = []
    nodes = [f"srv-{i:02d}" for i in range(1, 6)]
    for _ in range(6):
        a, b = random.sample(nodes, 2)
        edges.append({"from": a, "to": b})
    return {"items": edges}

# ---------------------------------------------
# Cisco Catalyst Center (DNAC)
# ---------------------------------------------
@app.post("/dna/system/api/v1/auth/token")
async def catalyst_auth(request: Request):
    # DNAC token via Basic Auth (username/password); respond with token JSON and header
    response = {"Token": fake.uuid4()}
    return response

@app.get("/dna/intent/api/v1/network-health")
async def catalyst_network_health():
    return [{
        "networkHealthAverage": round(random.uniform(60, 98), 1),
        "healthScore": round(random.uniform(60, 98), 1),
        "time": _now_iso(),
    }]

@app.get("/dna/intent/api/v1/client-health")
async def catalyst_client_health():
    # Return per-site client health
    sites = [f"site-{i:02d}" for i in range(1, 4)]
    return [{"site": s, "healthScore": round(random.uniform(55, 95), 1)} for s in sites]

@app.get("/dna/intent/api/v1/device-health")
async def catalyst_device_health():
    devices = []
    for _ in range(4):
        devices.append({
            "hostname": fake.hostname(),
            "managementIpAddr": _rand_ip(),
            "overallHealth": round(random.uniform(60, 99), 1),
        })
    return devices

@app.get("/dna/intent/api/v1/events")
async def catalyst_events():
    severities = ["info", "minor", "major", "critical"]
    return [{
        "name": f"event-{i}",
        "severity": random.choice(severities),
        "device": fake.hostname(),
        "device_ip": _rand_ip(),
        "time": _now_iso(),
    } for i in range(5)]
```

## 3) Configure your Data Sources to use the mock API

Use the existing sources API to create and enable producers pointing at `http://localhost:8085`.

### SCOM (REST)

```bash
curl -s -X POST "$API/api/v1/sources/" -H "Content-Type: application/json" -d @- <<'JSON'
{
  "name": "mock-scom",
  "type": "scom",
  "enabled": true,
  "config": {
    "base_url": "http://localhost:8085",
    "domain": "CONTOSO",
    "username": "svc_scom",
    "password": "secret",
    "verify_ssl": false,
    "poll_seconds": 15
  }
}
JSON
```

### SquaredUp (API key)

```bash
curl -s -X POST "$API/api/v1/sources/" -H "Content-Type: application/json" -d @- <<'JSON'
{
  "name": "mock-squaredup",
  "type": "squaredup",
  "enabled": true,
  "config": {
    "base_url": "http://localhost:8085",
    "api_key": "demo-key",
    "poll_seconds": 15,
    "verify_ssl": false
  }
}
JSON
```

### Cisco Catalyst Center (DNAC)

```bash
curl -s -X POST "$API/api/v1/sources/" -H "Content-Type: application/json" -d @- <<'JSON'
{
  "name": "mock-catalyst",
  "type": "catalyst",
  "enabled": true,
  "config": {
    "base_url": "http://localhost:8085",
    "username": "admin",
    "password": "Cisco123",
    "poll_seconds": 20,
    "verify_ssl": false
  }
}
JSON
```

## 4) Notes on Realism and Correlation

- The mock emits plausible fields (hostnames, IPs, severities, counters) for each system. Shapes align with the producers and normalizers you already have:
  - SCOM: alerts/performance/events → scom.* metrics
  - SquaredUp: health/alerts/dependencies → squaredup.* metrics
  - Catalyst: network/client/device health and events → cisco.cc.* metrics
- Cross-tool correlation demos:
  - Embedding graph: logs from all sources are written to `logs_*` and will appear in your correlation graph
  - Key-based: use `/api/v1/metrics/correlation/keys?keys=device_ip,client_mac,test_id` to show clusters by key across sources

## 5) Troubleshooting

- Backend offline: ensure app is running with your standard command and that Redis/Chroma are reachable.
- No data: confirm sources are enabled, and that their `base_url` points to the mock (`http://localhost:8085`).
- TLS: the mock runs over HTTP; set `verify_ssl: false` in source configs.

## 6) Optional Extensions

- Add `/v6/alerts.json` and `/v6/tests.json` mock endpoints to emulate ThousandEyes if you want to also synthesize that data from the same server.
- Add `/Services/REST/v1/events` shapes for BlueCat; the BlueCat producer and normalizer are already in place to ingest JSON arrays (`{"items":[...]}` or `[...]`).




