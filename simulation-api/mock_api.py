from __future__ import annotations

import asyncio
import base64
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import json
import os
from pathlib import Path
from dateutil import tz
from fastapi import FastAPI, Header, Request, Response, Body
from pydantic import BaseModel
from faker import Faker
import httpx


# -----------------------------
# Configuration knobs
# -----------------------------
SIM_HOURS_PER_TICK: float = 24.0
DAMAGE_MULTIPLIER: float = 5000.0
SEED_RANDOM: Optional[int] = 42
SIM_USE_LLM: bool = True

# -----------------------------
# App and globals
# -----------------------------
fake = Faker()
if SEED_RANDOM is not None:
    random.seed(SEED_RANDOM)

app = FastAPI(title="Stateful Simulation API")


Status = str  # "OPERATIONAL" | "DEGRADED" | "FAILED"


@dataclass
class SimComponent:
    comp_id: str
    name: str
    component_type: str  # e.g., cpu, memory, disk, nic, motherboard, psu, fan, sensor
    beta: float = 2.0
    eta: float = 2000.0
    health: float = 100.0
    age_hours: float = 0.0
    status: Status = "OPERATIONAL"
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SimulatedCI:
    ci_id: str
    name: str
    ci_type: str  # e.g., server, db, app, thousandeyes_test
    beta: float = 2.0
    eta: float = 2000.0  # hours characteristic life
    health: float = 100.0
    age_hours: float = 0.0
    status: Status = "OPERATIONAL"
    depends_on: List[str] = field(default_factory=list)
    ip_address: Optional[str] = None
    open_ports: List[int] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    components: List[SimComponent] = field(default_factory=list)


# CMDB: id -> CI
CMDB: Dict[str, SimulatedCI] = {}

# Communication pairs for NetFlow generation: (src_ci_id, dst_ci_id, dst_port)
COMMUNICATIONS: List[Tuple[str, str, int]] = []

# Flow cache (array response)
FLOWS: List[Dict[str, Any]] = []
FLOWS_MAX: int = 300

# Simulation controls
_PAUSED: bool = False
_SPEC_PATH_ENV: str = "SIM_SPEC_PATH"
_OLLAMA_URL: Optional[str] = None
_OLLAMA_MODEL: str = "llama3.2:3b"


# -----------------------------
# Utilities
# -----------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rand_ip() -> str:
    return ".".join(str(random.randint(1, 254)) for _ in range(4))


def _worst_status(statuses: List[Status]) -> Status:
    if any(s == "FAILED" for s in statuses):
        return "FAILED"
    if any(s == "DEGRADED" for s in statuses):
        return "DEGRADED"
    return "OPERATIONAL"


def _status_from_health(health: float) -> Status:
    if health <= 0:
        return "FAILED"
    if health <= 50:
        return "DEGRADED"
    return "OPERATIONAL"


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _load_spec() -> Dict[str, Any]:
    """Load spec JSON from path.
    Priority:
      1) SIM_SPEC_PATH env (absolute or relative)
      2) ./spec.json in this directory
      3) ./spec.example.json in this directory
    """
    here = Path(__file__).resolve().parent
    candidates: List[Path] = []
    env_path = os.environ.get(_SPEC_PATH_ENV)
    if env_path:
        p = Path(env_path)
        if not p.is_absolute():
            p = (here / p).resolve()
        candidates.append(p)
    candidates.append((here / "spec.json").resolve())
    candidates.append((here / "spec.example.json").resolve())
    for cand in candidates:
        try:
            if cand.exists():
                with cand.open("r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            continue
    return {"cis": [], "communications": [], "sim": {}}


def _load_parent_env_var(name: str) -> Optional[str]:
    """Load a variable from parent project's .env if present."""
    try:
        here = Path(__file__).resolve().parent
        env_path = (here.parent / ".env")
        if not env_path.exists():
            return None
        val: Optional[str] = None
        with env_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == name:
                    val = v
                    break
        return val
    except Exception:
        return None


def initialize_cmdb(spec: Optional[Dict[str, Any]] = None) -> None:
    CMDB.clear()
    COMMUNICATIONS.clear()
    FLOWS.clear()

    loaded = spec if spec is not None else _load_spec()

    # Apply simulation settings
    global SIM_HOURS_PER_TICK, DAMAGE_MULTIPLIER, SEED_RANDOM, FLOWS_MAX, SIM_USE_LLM
    sim_cfg: Dict[str, Any] = loaded.get("sim", {}) or {}
    if "hours_per_tick" in sim_cfg:
        SIM_HOURS_PER_TICK = float(sim_cfg.get("hours_per_tick"))
    if "damage_multiplier" in sim_cfg:
        DAMAGE_MULTIPLIER = float(sim_cfg.get("damage_multiplier"))
    if "seed" in sim_cfg:
        SEED_RANDOM = int(sim_cfg.get("seed"))
        random.seed(SEED_RANDOM)
    if "flows_max" in sim_cfg:
        FLOWS_MAX = int(sim_cfg.get("flows_max"))
    if "use_llm" in sim_cfg:
        SIM_USE_LLM = bool(sim_cfg.get("use_llm"))

    # Build CIs
    for obj in loaded.get("cis", []):
        ci = SimulatedCI(
            ci_id=str(obj.get("ci_id")),
            name=str(obj.get("name") or obj.get("ci_id")),
            ci_type=str(obj.get("ci_type")),
            beta=float(obj.get("beta", 2.0)),
            eta=float(obj.get("eta", 2000.0)),
            health=float(obj.get("health", 100.0)),
            age_hours=float(obj.get("age_hours", 0.0)),
            status=str(obj.get("status", "OPERATIONAL")),
            depends_on=[str(x) for x in (obj.get("depends_on") or [])],
            ip_address=str(obj.get("ip_address")) if obj.get("ip_address") else _rand_ip(),
            open_ports=[int(p) for p in (obj.get("open_ports") or [])],
            meta=dict(obj.get("meta") or {}),
        )
        # Components
        comps: List[SimComponent] = []
        for c in obj.get("components", []) or []:
            comps.append(
                SimComponent(
                    comp_id=str(c.get("comp_id") or f"{ci.ci_id}:{c.get('component_type','comp')}"),
                    name=str(c.get("name") or str(c.get("component_type") or "component")),
                    component_type=str(c.get("component_type") or "component"),
                    beta=float(c.get("beta", 2.0)),
                    eta=float(c.get("eta", 2000.0)),
                    health=float(c.get("health", 100.0)),
                    age_hours=float(c.get("age_hours", 0.0)),
                    status=str(c.get("status", "OPERATIONAL")),
                    meta=dict(c.get("meta") or {}),
                )
            )
        ci.components = comps
        CMDB[ci.ci_id] = ci

    # Communications
    for comm in loaded.get("communications", []):
        src = str(comm.get("src"))
        dst = str(comm.get("dst"))
        dport = int(comm.get("dst_port", 0))
        if src and dst and dport:
            COMMUNICATIONS.append((src, dst, dport))


async def _simulation_tick() -> None:
    global FLOWS
    while True:
        try:
            if not _PAUSED:
                # Advance time and degrade health (Weibull-like)
                for ci in CMDB.values():
                    # Advance CI age and compute CI-level damage
                    ci.age_hours += SIM_HOURS_PER_TICK
                    base_damage = DAMAGE_MULTIPLIER * ((ci.age_hours / max(ci.eta, 1.0)) ** ci.beta)
                    base_health = _clamp(100.0 - base_damage, 0.0, 100.0)

                    # Components aging and damage
                    comp_min_health = 100.0
                    comp_statuses: List[Status] = []
                    for comp in ci.components:
                        comp.age_hours += SIM_HOURS_PER_TICK
                        c_damage = DAMAGE_MULTIPLIER * ((comp.age_hours / max(comp.eta, 1.0)) ** comp.beta)
                        comp.health = _clamp(100.0 - c_damage, 0.0, 100.0)
                        comp.status = _status_from_health(comp.health)
                        comp_min_health = min(comp_min_health, comp.health)
                        comp_statuses.append(comp.status)

                    # Combine CI health with components (worst-of)
                    if ci.components:
                        ci.health = min(base_health, comp_min_health)
                        ci.status = _worst_status([_status_from_health(ci.health)] + comp_statuses)
                    else:
                        ci.health = base_health
                        ci.status = _status_from_health(ci.health)

                # Propagate dependency status (worst-of children)
                for ci in CMDB.values():
                    if ci.depends_on:
                        deps = [CMDB[d].status for d in ci.depends_on if d in CMDB]
                        if deps:
                            ci.status = _worst_status([ci.status] + deps)

                # Generate NetFlow records based on communications
                flows_now: List[Dict[str, Any]] = []
                for src_id, dst_id, dport in COMMUNICATIONS:
                    src = CMDB.get(src_id)
                    dst = CMDB.get(dst_id)
                    if not src or not dst:
                        continue
                    # Consider NIC component state if present
                    def nic_state(ci: SimulatedCI) -> Status:
                        nics = [c for c in ci.components if c.component_type.lower() in {"nic", "network", "ethernet"}]
                        if not nics:
                            return ci.status
                        return _worst_status([x.status for x in nics])

                    s_state = nic_state(src)
                    d_state = nic_state(dst)
                    if "FAILED" in {s_state, d_state}:
                        continue
                    # Bytes scale by status
                    if "DEGRADED" in {s_state, d_state}:
                        bytes_count = random.randint(10_000, 80_000)
                    else:
                        bytes_count = random.randint(400_000, 2_000_000)
                    flows_now.append(
                        {
                            "timestamp": _now_iso(),
                            "src_ip": src.ip_address or _rand_ip(),
                            "dst_ip": dst.ip_address or _rand_ip(),
                            "src_port": random.randint(1024, 65535),
                            "dst_port": dport,
                            "protocol": "tcp",
                            "bytes": bytes_count,
                        }
                    )
                if flows_now:
                    FLOWS.extend(flows_now)
                    if len(FLOWS) > FLOWS_MAX:
                        FLOWS = FLOWS[-FLOWS_MAX:]
        except Exception:
            # Keep ticking even if one iteration fails
            pass
        await asyncio.sleep(1.0)


@app.on_event("startup")
async def _startup() -> None:
    # Determine Ollama URL: env overrides, else parent .env, else default
    global _OLLAMA_URL
    _OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL") or _load_parent_env_var("OLLAMA_BASE_URL") or "http://localhost:11434"
    initialize_cmdb()
    asyncio.create_task(_simulation_tick())


# -----------------------------
# Runtime controls
# -----------------------------
@app.post("/api/v1/sim/pause")
async def sim_pause() -> Dict[str, Any]:
    global _PAUSED
    _PAUSED = True
    return {"ok": True, "paused": _PAUSED}


@app.post("/api/v1/sim/resume")
async def sim_resume() -> Dict[str, Any]:
    global _PAUSED
    _PAUSED = False
    return {"ok": True, "paused": _PAUSED}


@app.post("/api/v1/sim/reset")
async def sim_reset() -> Dict[str, Any]:
    initialize_cmdb()
    return {"ok": True}


@app.post("/api/v1/sim/speed")
async def sim_speed(hours_per_tick: Optional[float] = None, damage_multiplier: Optional[float] = None) -> Dict[str, Any]:
    global SIM_HOURS_PER_TICK, DAMAGE_MULTIPLIER
    if hours_per_tick is not None:
        SIM_HOURS_PER_TICK = float(hours_per_tick)
    if damage_multiplier is not None:
        DAMAGE_MULTIPLIER = float(damage_multiplier)
    return {"ok": True, "SIM_HOURS_PER_TICK": SIM_HOURS_PER_TICK, "DAMAGE_MULTIPLIER": DAMAGE_MULTIPLIER}


# -----------------------------
# LLM (Ollama) for log generation
# -----------------------------
async def _gen_text(prompt: str) -> str:
    if not SIM_USE_LLM:
        return fake.sentence(nb_words=10)
    base = os.environ.get("OLLAMA_BASE_URL") or _OLLAMA_URL
    if not base:
        return fake.sentence(nb_words=10)
    url = f"{base.rstrip('/')}/api/generate"
    payload = {"model": _OLLAMA_MODEL, "prompt": prompt, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("response") or ""
            return (text or "").strip()[:300] or fake.sentence(nb_words=10)
    except Exception:
        return fake.sentence(nb_words=10)


# -----------------------------
# LLM (Ollama) JSON-mode helpers
# -----------------------------
async def _ollama_chat_json(messages: List[Dict[str, str]], *, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    base = os.environ.get("OLLAMA_BASE_URL") or _OLLAMA_URL
    if not base:
        return None
    url = f"{base.rstrip('/')}/api/chat"
    payload = {
        "model": _OLLAMA_MODEL,
        "messages": messages,
        "options": {"temperature": 0.2, "top_p": 0.95, "format": "json"},
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            msg = (data.get("message") or {}).get("content") or ""
            if not msg:
                return None
            try:
                return json.loads(msg)
            except Exception:
                msg_str = str(msg).strip()
                if msg_str.startswith("```"):
                    msg_str = msg_str.strip("`")
                    parts = msg_str.split("\n", 1)
                    if len(parts) == 2 and parts[0].strip().lower() in {"json", "javascript"}:
                        msg_str = parts[1]
                try:
                    return json.loads(msg_str)
                except Exception:
                    return None
    except Exception:
        return None


def _coerce_float(x: Any, lo: float, hi: float, default: float) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return _clamp(v, lo, hi)
    except Exception:
        return default


def _coerce_str(x: Any, default: str) -> str:
    try:
        s = str(x).strip()
        return s if s else default
    except Exception:
        return default


async def _gen_title(prefix: str, ci_name: str, status: str) -> str:
    if not SIM_USE_LLM:
        return f"{prefix} {ci_name} {status.lower()}"
    messages = [
        {"role": "system", "content": "Return JSON: {\"title\":\"...\"}. Keep under 6 words; no trailing punctuation."},
        {"role": "user", "content": json.dumps({"prefix": prefix, "name": ci_name, "status": status})},
    ]
    data = await _ollama_chat_json(messages)
    if isinstance(data, dict):
        t = _coerce_str(data.get("title"), "")
        if t:
            return t
    return f"{prefix} {ci_name} {status.lower()}"


async def _gen_message(channel: str, ci_name: str, detail: str) -> str:
    if not SIM_USE_LLM:
        return f"{ci_name} {detail}"
    messages = [
        {"role": "system", "content": "Return JSON: {\"message\":\"...\"}. 10-18 words, realistic ops log phrasing."},
        {"role": "user", "content": json.dumps({"channel": channel, "host": ci_name, "detail": detail})},
    ]
    data = await _ollama_chat_json(messages)
    if isinstance(data, dict):
        t = _coerce_str(data.get("message"), "")
        if t:
            return t
    return f"{ci_name} {detail}"


async def _gen_te_alert(ci_name: str, target_status: str) -> Dict[str, Any]:
    if not SIM_USE_LLM:
        severity = "warning" if target_status == "DEGRADED" else "critical"
        return {
            "ruleName": "Performance threshold exceeded",
            "testName": ci_name,
            "severity": severity,
            "summary": f"Target status {target_status}",
            "startTime": _now_iso(),
        }
    fewshot = {
        "ruleName": "Performance threshold exceeded",
        "testName": "HTTP Test - cart",
        "severity": "warning",
        "summary": "Elevated latency and packet loss",
        "startTime": "2025-11-13T12:00:00Z",
    }
    messages = [
        {"role": "system", "content": "You generate ThousandEyes alert JSON objects with the exact keys shown. Keep values realistic."},
        {"role": "user", "content": json.dumps({"example": fewshot})},
        {"role": "user", "content": json.dumps({"request": {"testName": ci_name, "status": target_status}})},
    ]
    data = await _ollama_chat_json(messages)
    severity = "warning" if target_status == "DEGRADED" else "critical"
    if not isinstance(data, dict):
        return {
            "ruleName": "Performance threshold exceeded",
            "testName": ci_name,
            "severity": severity,
            "summary": f"Target status {target_status}",
            "startTime": _now_iso(),
        }
    return {
        "ruleName": _coerce_str(data.get("ruleName"), "Performance threshold exceeded"),
        "testName": _coerce_str(data.get("testName"), ci_name),
        "severity": _coerce_str(data.get("severity"), severity),
        "summary": _coerce_str(data.get("summary"), f"Target status {target_status}"),
        "startTime": _coerce_str(data.get("startTime"), _now_iso()),
    }


async def _gen_te_test(ci_name: str, target_status: str) -> Dict[str, Any]:
    if not SIM_USE_LLM:
        if target_status == "OPERATIONAL":
            latency, loss, availability = random.uniform(30, 80), random.uniform(0.0, 0.3), random.uniform(99.0, 100.0)
        elif target_status == "DEGRADED":
            latency, loss, availability = random.uniform(150, 400), random.uniform(1.0, 5.0), random.uniform(92.0, 97.0)
        else:
            latency, loss, availability = random.uniform(400, 1000), random.uniform(10.0, 30.0), random.uniform(0.0, 60.0)
        return {
            "testId": abs(hash(ci_name)) % 100000,
            "testName": ci_name,
            "type": "http-server",
            "metrics": {
                "latencyMs": round(latency, 1),
                "loss": round(loss, 2),
                "availability": round(availability, 1),
            },
        }
    fewshot = {
        "testId": 10123,
        "testName": "HTTP Test - cart",
        "type": "http-server",
        "metrics": {"latencyMs": 245.7, "loss": 1.8, "availability": 94.2},
    }
    messages = [
        {"role": "system", "content": "You generate ThousandEyes test JSON objects. Keep metrics realistic and within bounds."},
        {"role": "user", "content": json.dumps({"example": fewshot})},
        {"role": "user", "content": json.dumps({"request": {"testName": ci_name, "status": target_status}})},
    ]
    data = await _ollama_chat_json(messages)
    if not isinstance(data, dict):
        return await _gen_te_test(ci_name, target_status="OPERATIONAL")
    metrics = data.get("metrics") or {}
    if target_status == "OPERATIONAL":
        lat_lo, lat_hi = 20.0, 120.0
        loss_lo, loss_hi = 0.0, 0.5
        avail_lo, avail_hi = 98.0, 100.0
    elif target_status == "DEGRADED":
        lat_lo, lat_hi = 120.0, 500.0
        loss_lo, loss_hi = 0.5, 6.0
        avail_lo, avail_hi = 90.0, 98.0
    else:
        lat_lo, lat_hi = 300.0, 1500.0
        loss_lo, loss_hi = 5.0, 50.0
        avail_lo, avail_hi = 0.0, 80.0
    return {
        "testId": int(data.get("testId") or abs(hash(ci_name)) % 100000),
        "testName": _coerce_str(data.get("testName"), ci_name),
        "type": _coerce_str(data.get("type"), "http-server"),
        "metrics": {
            "latencyMs": round(_coerce_float(metrics.get("latencyMs"), lat_lo, lat_hi, lat_hi - 1), 1),
            "loss": round(_coerce_float(metrics.get("loss"), loss_lo, loss_hi, loss_lo + 0.1), 2),
            "availability": round(_coerce_float(metrics.get("availability"), avail_lo, avail_hi, avail_hi - 1), 1),
        },
    }


# -----------------------------
# SCOM (System Center Operations Manager)
# -----------------------------
@app.post("/OperationsManager/authenticate")
async def scom_authenticate(request: Request):
    # Body is often JSON string of base64-encoded "(Network):DOMAIN\\username:password"
    decoded_user = "(Network):mock\\user:pass"
    try:
        raw = await request.body()
        if not raw:
            return {"status": "ok", "user": decoded_user}
        text = raw.decode("utf-8", errors="ignore").strip()
        # If JSON string, strip quotes
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        # Attempt base64 decode
        try:
            decoded_user = base64.b64decode(text).decode("utf-8", errors="ignore") or decoded_user
        except Exception:
            # If not base64, accept raw
            decoded_user = text or decoded_user
    except Exception:
        pass
    return {"status": "ok", "user": decoded_user}


@app.get("/OperationsManager")
async def scom_init_csrf(response: Response):
    response.headers["X-CSRF-Token"] = fake.uuid4()
    return {"ok": True}


@app.post("/OperationsManager/data/alert")
async def scom_alerts(_: Any = Body(default=None)):
    items = []
    for ci in CMDB.values():
        if ci.status == "OPERATIONAL":
            continue
        sev = "Warning" if ci.status == "DEGRADED" else "Error"
        title = await _gen_title("Alert", ci.name, ci.status)
        items.append(
            {
                "Id": fake.uuid4(),
                "Name": title or f"{ci.name} {sev.lower()}",
                "Severity": sev,
                "Priority": random.choice(["Low", "Medium", "High"]),
                "MonitoringObjectDisplayName": ci.name,
                "LastModified": _now_iso(),
            }
        )
    # Include a few background infos
    for _ in range(random.randint(1, 3)):
        items.append(
            {
                "Id": fake.uuid4(),
                "Name": fake.sentence(nb_words=3),
                "Severity": "Information",
                "Priority": random.choice(["Low", "Medium"]),
                "MonitoringObjectDisplayName": fake.hostname(),
                "LastModified": _now_iso(),
            }
        )
    return {"items": items}


@app.post("/OperationsManager/data/performance")
async def scom_performance(_: Any = Body(default=None)):
    items = []
    for ci in CMDB.values():
        if ci.ci_type not in {"server", "db"}:
            continue
        # Prefer component-specific metrics if present
        cpu_comp = next((c for c in ci.components if c.component_type.lower() == "cpu"), None)
        mem_comp = next((c for c in ci.components if c.component_type.lower() in {"memory", "ram"}), None)
        disk_comp = next((c for c in ci.components if c.component_type.lower() in {"disk", "storage"}), None)

        base_h = ci.health
        cpu_h = cpu_comp.health if cpu_comp else base_h
        mem_h = mem_comp.health if mem_comp else base_h
        disk_h = disk_comp.health if disk_comp else base_h

        cpu = 100.0 - cpu_h + random.uniform(0, 5)
        mem_avail = max(50.0, 16000.0 * (mem_h / 100.0) + random.uniform(-200.0, 200.0))
        disk_read = max(0.0, (100.0 - disk_h) / 1000.0 + random.uniform(0, 0.01))
        items.extend(
            [
                {
                    "ObjectName": "Processor",
                    "CounterName": "Processor Time",
                    "InstanceName": random.choice(["_Total", "0"]),
                    "Value": round(_clamp(cpu, 0.0, 100.0), 2),
                    "ComputerName": ci.name,
                    "Timestamp": _now_iso(),
                },
                {
                    "ObjectName": "Memory",
                    "CounterName": "Available MBytes",
                    "InstanceName": "",
                    "Value": round(_clamp(mem_avail, 0.0, 64000.0), 2),
                    "ComputerName": ci.name,
                    "Timestamp": _now_iso(),
                },
                {
                    "ObjectName": "LogicalDisk",
                    "CounterName": "Avg. Disk sec/Read",
                    "InstanceName": random.choice(["C:", "sda"]),
                    "Value": round(disk_read, 4),
                    "ComputerName": ci.name,
                    "Timestamp": _now_iso(),
                },
            ]
        )
    return {"items": items}


@app.post("/OperationsManager/data/event")
async def scom_events(_: Any = Body(default=None)):
    items = []
    for ci in CMDB.values():
        if ci.status == "OPERATIONAL":
            continue
        level = "Warning" if ci.status == "DEGRADED" else "Error"
        # If any critical component failed, emit component-specific event
        critical_types = {"motherboard", "psu", "fan", "cpu", "memory", "nic"}
        emitted = False
        for comp in ci.components:
            if comp.component_type.lower() in critical_types and comp.status != "OPERATIONAL":
                msg = await _gen_message("Hardware", ci.name, f"component {comp.component_type} {comp.status}")
                items.append(
                    {
                        "LevelDisplayName": "Error" if comp.status == "FAILED" else "Warning",
                        "ComputerName": ci.name,
                        "Channel": "Hardware",
                        "Message": msg,
                        "TimeGenerated": _now_iso(),
                    }
                )
                emitted = True
        if not emitted:
            msg = await _gen_message("Application", ci.name, f"status {ci.status}")
            items.append(
                {
                    "LevelDisplayName": level,
                    "ComputerName": ci.name,
                    "Channel": "Application",
                    "Message": msg,
                    "TimeGenerated": _now_iso(),
                }
            )
    return {"items": items}


# -----------------------------
# SquaredUp (API key)
# -----------------------------
def _require_api_key(x_api_key: Optional[str]) -> None:
    # No-op enforcement for demo; accept any provided/empty
    return None


@app.get("/api/health")
async def squaredup_health(x_api_key: Optional[str] = Header(None, convert_underscores=False)):
    _require_api_key(x_api_key)
    items = []
    for ci in CMDB.values():
        if ci.ci_type not in {"app", "server", "db"}:
            continue
        state = "ok" if ci.status == "OPERATIONAL" else ("degraded" if ci.status == "DEGRADED" else "critical")
        items.append({"name": ci.name, "state": state, "updated": _now_iso()})
    return {"items": items}


@app.get("/api/alerts")
async def squaredup_alerts(x_api_key: Optional[str] = Header(None, convert_underscores=False)):
    _require_api_key(x_api_key)
    severities = {"OPERATIONAL": "info", "DEGRADED": "warning", "FAILED": "critical"}
    items = []
    for ci in CMDB.values():
        if ci.status == "OPERATIONAL":
            continue
        title = await _gen_title("Incident", ci.name, ci.status)
        items.append(
            {
                "id": fake.uuid4(),
                "title": title,
                "severity": severities[ci.status],
                "created": _now_iso(),
            }
        )
    return {"items": items}


@app.get("/api/dependencies")
async def squaredup_dependencies(x_api_key: Optional[str] = Header(None, convert_underscores=False)):
    _require_api_key(x_api_key)
    edges = []
    for ci in CMDB.values():
        for dep in ci.depends_on:
            edges.append({"from": dep, "to": ci.ci_id})
    return {"items": edges}


# -----------------------------
# Cisco Catalyst Center (DNAC)
# -----------------------------
@app.post("/dna/system/api/v1/auth/token")
async def catalyst_auth(_: Request):
    return {"Token": fake.uuid4()}


@app.get("/dna/intent/api/v1/network-health")
async def catalyst_network_health():
    # Rough aggregation across infra nodes
    scores = []
    for ci in CMDB.values():
        if ci.ci_type in {"server", "db"}:
            if ci.status == "OPERATIONAL":
                scores.append(random.uniform(90, 99))
            elif ci.status == "DEGRADED":
                scores.append(random.uniform(70, 85))
            else:
                scores.append(random.uniform(20, 40))
    avg = round(sum(scores) / len(scores), 1) if scores else round(random.uniform(60, 98), 1)
    return [{"networkHealthAverage": avg, "healthScore": avg, "time": _now_iso()}]


@app.get("/dna/intent/api/v1/client-health")
async def catalyst_client_health():
    # Keep simple demo sites driven by network health bias
    sites = [f"site-{i:02d}" for i in range(1, 4)]
    items = []
    for s in sites:
        base = 85.0
        if any(ci.status != "OPERATIONAL" for ci in CMDB.values() if ci.ci_type in {"server", "db"}):
            base = 72.0
        items.append({"site": s, "healthScore": round(random.uniform(base - 10, base + 10), 1)})
    return items


@app.get("/dna/intent/api/v1/device-health")
async def catalyst_device_health():
    devices = []
    for ci in CMDB.values():
        if ci.ci_type not in {"server", "db"}:
            continue
        if ci.status == "OPERATIONAL":
            score = random.uniform(90, 99)
        elif ci.status == "DEGRADED":
            score = random.uniform(65, 85)
        else:
            score = random.uniform(20, 45)
        devices.append({"hostname": ci.name, "managementIpAddr": ci.ip_address or _rand_ip(), "overallHealth": round(score, 1)})
    return devices


@app.get("/dna/intent/api/v1/events")
async def catalyst_events():
    severities = ["info", "minor", "major", "critical"]
    out = []
    for ci in CMDB.values():
        sev = "info"
        if ci.status == "DEGRADED":
            sev = "major"
        elif ci.status == "FAILED":
            sev = "critical"
        name = await _gen_title("Event", ci.ci_id, ci.status)
        out.append({"name": name, "severity": sev, "device": ci.name, "device_ip": ci.ip_address or _rand_ip(), "time": _now_iso()})
    return out


# -----------------------------
# ThousandEyes (extended mode)
# -----------------------------
def _check_te_auth(authorization: Optional[str], x_te_auth_token: Optional[str]) -> None:
    # Accept any for demo; presence is enough
    return None


@app.get("/v6/alerts.json")
async def te_alerts(window: Optional[str] = None, authorization: Optional[str] = Header(None), x_te_auth_token: Optional[str] = Header(None, convert_underscores=False)):
    _check_te_auth(authorization, x_te_auth_token)
    alerts: List[Dict[str, Any]] = []
    for ci in CMDB.values():
        if ci.ci_type != "thousandeyes_test":
            continue
        # Look at target status
        target_status = "OPERATIONAL"
        if ci.depends_on:
            deps = [CMDB[d].status for d in ci.depends_on if d in CMDB]
            target_status = _worst_status(deps) if deps else "OPERATIONAL"
        if target_status == "OPERATIONAL":
            continue
        alerts.append(await _gen_te_alert(ci.name, target_status))
    return {"alerts": alerts}


@app.get("/v6/tests.json")
async def te_tests(authorization: Optional[str] = Header(None), x_te_auth_token: Optional[str] = Header(None, convert_underscores=False)):
    _check_te_auth(authorization, x_te_auth_token)
    tests: List[Dict[str, Any]] = []
    for ci in CMDB.values():
        if ci.ci_type != "thousandeyes_test":
            continue
        # Target app/server
        target_status = "OPERATIONAL"
        if ci.depends_on:
            deps = [CMDB[d].status for d in ci.depends_on if d in CMDB]
            target_status = _worst_status(deps) if deps else "OPERATIONAL"
        tests.append(await _gen_te_test(ci.name, target_status))
    return {"tests": tests}


# -----------------------------
# NetFlow (demo feed)
# -----------------------------
@app.get("/api/v1/netflow")
async def netflow_feed():
    # Return as array of flow objects for simplicity
    return FLOWS[-FLOWS_MAX:]


# Root
@app.get("/")
async def root():
    return {"service": "stateful-sim", "now": _now_iso(), "paused": _PAUSED}


