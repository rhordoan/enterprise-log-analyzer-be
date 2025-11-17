# SCOM and SquaredUp Integration

## SCOM (REST API)

Producer type: `scom`

Config:
```json
{
  "base_url": "https://scom-server",
  "domain": "CONTOSO",
  "username": "svc_scom",
  "password": "*****",
  "verify_ssl": true,
  "poll_seconds": 30,
  "alerts_path": "/OperationsManager/data/alert",
  "perf_path": "/OperationsManager/data/performance",
  "events_path": "/OperationsManager/data/event",
  "criteria": {
    "alerts": "LastModified > '2025-01-01T00:00:00Z'",
    "perf": "",
    "events": ""
  }
}
```

Notes:
- The producer authenticates via `/OperationsManager/authenticate` and maintains session cookies. SCOM versions requiring CSRF will be initialized automatically.
- Items are pushed to the Redis `logs` stream with `source="scom:<hostname>"` as JSON lines.
- The consumer normalizes SCOM payloads into OpenTelemetry-like metrics (alerts, performance, events) when `ENABLE_METRICS_NORMALIZATION=true`.

Create via API:
```bash
curl -s -X POST "$API/api/v1/sources/" -H "Content-Type: application/json" -d @- <<'JSON'
{
  "name": "scom-primary",
  "type": "scom",
  "enabled": true,
  "config": {
    "base_url": "https://scom-server",
    "domain": "CONTOSO",
    "username": "svc_scom",
    "password": "secret",
    "verify_ssl": true,
    "poll_seconds": 30
  }
}
JSON
```

## SquaredUp (API Key)

Producer type: `squaredup`

Config:
```json
{
  "base_url": "https://squaredup",
  "api_key": "squp_***",
  "header_name": "X-Api-Key",
  "verify_ssl": true,
  "poll_seconds": 30,
  "health_path": "/api/health",
  "alerts_path": "/api/alerts",
  "deps_path": "/api/dependencies"
}
```

Create via API:
```bash
curl -s -X POST "$API/api/v1/sources/" -H "Content-Type: application/json" -d @- <<'JSON'
{
  "name": "squaredup-main",
  "type": "squaredup",
  "enabled": true,
  "config": {
    "base_url": "https://squaredup",
    "api_key": "squp_xxx",
    "poll_seconds": 30
  }
}
JSON
```

## Normalization
- SCOM metrics are mapped to:
  - Performance: `scom.perf.<object>.<counter> {instance?}`
  - Alerts: `scom.alert.severity` (0/1/2)
  - Events: `scom.event.count`
- SquaredUp:
  - Health: `squaredup.health.ok` (1/0, with `state` attribute)
  - Alerts: `squaredup.alert.severity`
  - Dependency: `squaredup.dependency.edge.count`

These appear in the Metrics Export UI and are sent to OTLP if enabled.

## Incidents and OS mapping

- Incidents derived from SCOM and SquaredUp are classified under `windows`.
- Examples of templated incident summaries:
  - SCOM alert: `scom alert critical SQL Server service stopped source=DB01`
  - SquaredUp alert: `squaredup alert warning Disk space low`
  - SquaredUp health (degraded): `squaredup health red WebApp`

Notes:
- If an incoming SCOM/SquaredUp item does not match a specific rule, a generic incident is still published with a concise summary and `os=windows`.
- Logs from `source` prefixed with `scom:` or `squaredup:` are also mapped to `windows` by the logs issues aggregator.


