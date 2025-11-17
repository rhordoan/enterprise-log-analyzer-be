# Cisco Integrations: Catalyst Center and ThousandEyes

## Catalyst Center (DNAC)

Producer type: `catalyst`

Config:
```json
{
  "base_url": "https://dnac",
  "username": "admin",
  "password": "*****",
  "verify_ssl": true,
  "poll_seconds": 30,
  "auth_path": "/dna/system/api/v1/auth/token",
  "health_paths": {
    "network": "/dna/intent/api/v1/network-health",
    "client": "/dna/intent/api/v1/client-health",
    "device": "/dna/intent/api/v1/device-health"
  },
  "events_path": "/dna/intent/api/v1/events"
}
```

Create via API:
```bash
curl -s -X POST "$API/api/v1/sources/" -H "Content-Type: application/json" -d @- <<'JSON'
{
  "name": "catalyst-primary",
  "type": "catalyst",
  "enabled": true,
  "config": {
    "base_url": "https://dnac",
    "username": "admin",
    "password": "secret",
    "poll_seconds": 30
  }
}
JSON
```

## ThousandEyes

Producer type: `thousandeyes`

Config (extended mode):
```json
{
  "base_url": "https://api.thousandeyes.com",
  "bearer_token": "te_***",
  "verify_ssl": true,
  "poll_interval_sec": 15,
  "alerts_path": "/v6/alerts.json",
  "tests_path": "/v6/tests.json",
  "window": "5m"
}
```

Back-compat (single-path):
```json
{ "base_url": "https://api.thousandeyes.com", "path": "/v6/alerts.json", "window": "5m" }
```

## NetFlow via Telegraf

- Use Telegraf `inputs.netflow` and send to the platform's Telegraf HTTP endpoint (`/api/v1/telemetry/telegraf`). The existing `telegraf` normalizer will emit generic metrics; correlation keys will extract `src_ip`/`dst_ip`.

## Correlation Keys Endpoint

- `GET /api/v1/metrics/correlation/keys?window_min=60&limit=2000&keys=device_ip,client_mac,test_id`
- Returns clusters grouped by key=value with per-source counts and sample events, enabling cross-tool correlation (SCOM, SquaredUp, Catalyst, ThousandEyes, NetFlow, etc.).




