# Stateful Mock API & Simulation Engine

This is a standalone FastAPI app that simulates a small infrastructure (“digital twin”) with stateful health degradation and correlated telemetry across multiple reporter endpoints (SCOM, SquaredUp, Cisco Catalyst Center, ThousandEyes, NetFlow).

## Quickstart

```bash
python -m venv .venv
# Linux/macOS
. .venv/bin/activate
# Windows
# .venv\Scripts\activate

pip install -r requirements.txt

uvicorn mock_api:app --host 0.0.0.0 --port 8085 --reload
```

Point your existing data sources to `http://localhost:8085` using your current mock producer configs.

## Spec-driven initialization

On startup, the app loads a spec JSON to build the CMDB and communications:
- Search order:
  1. `SIM_SPEC_PATH` env (absolute or relative to this folder)
  2. `./spec.json`
  3. `./spec.example.json`

Sample: `spec.example.json`.

Environment:
```bash
# Example of pointing to a custom spec
export SIM_SPEC_PATH=spec.json
```

## Configuration

Tune the simulation in `mock_api.py`:
- `SIM_HOURS_PER_TICK` (default: `24`)
- `DAMAGE_MULTIPLIER` (default: `5000`)
- `SEED_RANDOM` for reproducibility

Modify the infrastructure in `initialize_cmdb()` (add CIs, IPs, ports, dependencies, Weibull `beta`/`eta`).

## Endpoints (aligned with existing producers)
- SCOM: `/OperationsManager/authenticate`, `/OperationsManager`, `/OperationsManager/data/{alert|performance|event}`
- SquaredUp: `/api/health`, `/api/alerts`, `/api/dependencies`
- Catalyst: `/dna/system/api/v1/auth/token`, `/dna/intent/api/v1/{network-health|client-health|device-health|events}`
- ThousandEyes: `/v6/alerts.json`, `/v6/tests.json` (supports `Authorization: Bearer` or `X-TE-Auth-Token`, `?window=...`)
- NetFlow (optional demo feed): `/api/v1/netflow` returns an array of flow objects

## Runtime controls
- `POST /api/v1/sim/pause`
- `POST /api/v1/sim/resume`
- `POST /api/v1/sim/reset`
- `POST /api/v1/sim/speed?hours_per_tick=..&damage_multiplier=..`

## LLM-powered log generation (JSON mode)
- The simulator can generate realistic titles/messages using your Ollama base URL and model `llama3.2:3b`.
- It reads `OLLAMA_BASE_URL` from environment or from the parent `.env` file.
- JSON mode is used via `POST /api/chat` with `options.format=json` and few-shot examples per reporter.
- Toggle via spec: `"sim": { "use_llm": true }` (default true).


