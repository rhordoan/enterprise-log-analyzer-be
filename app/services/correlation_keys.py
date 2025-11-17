from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple
import json
import re

from app.core.config import settings
from app.services.chroma_service import ChromaClientProvider


IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}[-:]){5}([0-9A-Fa-f]{2})\b")


def _logs_collection_name(os_name: str) -> str:
    return f"{settings.CHROMA_LOG_COLLECTION_PREFIX}{os_name}"


def _extract_keys_from_json(obj: Dict[str, Any]) -> Dict[str, str]:
    keys: Dict[str, str] = {}
    # Common fields
    for k in ("device_ip", "device", "ip", "hostIp", "host_ip", "address"):
        v = obj.get(k)
        if isinstance(v, str) and IP_RE.search(v):
            keys["device_ip"] = IP_RE.search(v).group(0)  # type: ignore[union-attr]
            break
    for k in ("device_name", "host", "deviceHostname", "device_name"):
        v = obj.get(k)
        if isinstance(v, str) and v:
            keys["device_name"] = v
            break
    for k in ("interface", "ifName", "port", "ifname"):
        v = obj.get(k)
        if isinstance(v, str) and v:
            keys["interface"] = v
            break
    for k in ("client_mac", "mac", "clientMac"):
        v = obj.get(k)
        if isinstance(v, str) and MAC_RE.search(v):
            keys["client_mac"] = MAC_RE.search(v).group(0)  # type: ignore[union-attr]
            break
    for k in ("site", "siteName", "location"):
        v = obj.get(k)
        if isinstance(v, str) and v:
            keys["site"] = v
            break
    for k in ("test_id", "testId"):
        v = obj.get(k)
        if v is not None:
            keys["test_id"] = str(v)
            break
    for k in ("dst_ip", "dstIp", "destination", "destinationIp"):
        v = obj.get(k)
        if isinstance(v, str) and IP_RE.search(v):
            keys["dst_ip"] = IP_RE.search(v).group(0)  # type: ignore[union-attr]
            break
    for k in ("src_ip", "srcIp", "source", "sourceIp"):
        v = obj.get(k)
        if isinstance(v, str) and IP_RE.search(v):
            keys["src_ip"] = IP_RE.search(v).group(0)  # type: ignore[union-attr]
            break
    return keys


def compute_key_correlation(keys: List[str], limit: int = 2000) -> Dict[str, Any]:
    """Scan recent logs across OS collections, extract given keys, and group events sharing key values."""
    provider = ChromaClientProvider()
    events: List[Dict[str, Any]] = []
    for os_name in ("linux", "macos", "windows"):
        coll = provider.get_or_create_collection(_logs_collection_name(os_name))
        data = coll.get(include=["documents", "metadatas", "ids"], limit=int(limit)) or {}
        ids = data.get("ids") or []
        docs = data.get("documents") or []
        metas = data.get("metadatas") or []
        for i in range(len(ids)):
            doc = docs[i] if i < len(docs) else ""
            meta = metas[i] if i < len(metas) else {}
            obj: Dict[str, Any] = {}
            try:
                obj = json.loads(doc)
                if not isinstance(obj, dict):
                    obj = {}
            except Exception:
                # Not a JSON document; skip extraction
                continue
            ev = {
                "id": ids[i],
                "os": meta.get("os", os_name),
                "source": meta.get("source", ""),
                "data": obj,
            }
            events.append(ev)
    # Group by key=value
    groups: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        obj = ev["data"]
        extracted = _extract_keys_from_json(obj)
        for k in keys:
            val = extracted.get(k)
            if not val:
                continue
            gid = f"{k}={val}"
            g = groups.setdefault(gid, {"key": k, "value": val, "events": [], "sources": {}})
            g["events"].append({"id": ev["id"], "os": ev["os"], "source": ev["source"], "data": obj})
            src = ev["source"] or "unknown"
            g["sources"][src] = g["sources"].get(src, 0) + 1
    # Build clusters sorted by size
    clusters = sorted(groups.values(), key=lambda g: len(g["events"]), reverse=True)
    # Trim events per cluster for payload size
    for c in clusters:
        c["events"] = c["events"][:50]
    return {"clusters": clusters, "params": {"keys": keys, "limit": limit}}





