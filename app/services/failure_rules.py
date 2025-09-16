from __future__ import annotations

import re
from typing import Dict, List, Tuple


_RULES: List[Tuple[str, re.Pattern[str]]] = [
    (
        "disk",
        re.compile(
            r"\b(smart|reallocated|bad sector|io error|i/o error|seek error|read error|write error|fsck|filesystem error|disk failure|block error)\b",
            re.IGNORECASE,
        ),
    ),
    ("raid", re.compile(r"\b(raid degraded|mdadm|array degraded|rebuild failed|missing member)\b", re.IGNORECASE)),
    ("nvme", re.compile(r"\b(nvme fatal|nvme error|pci[e]? error|pcie bus error)\b", re.IGNORECASE)),
    ("thermal", re.compile(r"\b(overheat|thermal throttle|temperature limit|over temperature)\b", re.IGNORECASE)),
    ("memory", re.compile(r"\b(ecc error|corrected error|uncorrectable|memtest|oom killer)\b", re.IGNORECASE)),
    ("power", re.compile(r"\b(psu|power loss|brownout|undervoltage|overvoltage)\b", re.IGNORECASE)),
    ("cpu", re.compile(r"\b(mce|machine check|cpu stall|soft lockup|hard lockup)\b", re.IGNORECASE)),
    ("network", re.compile(r"\b(link down|carrier lost|nic failure|packet loss|rx/tx error)\b", re.IGNORECASE)),
]


def match_failure_signals(text: str) -> Dict[str, object]:
    """Return quick rule-based signal for potential hardware failures.

    Output shape:
    {"has_signal": bool, "label": str, "score": float, "evidence": List[str]}
    """
    text = text or ""
    evidence: List[str] = []
    labels: List[str] = []
    for label, pattern in _RULES:
        if pattern.search(text):
            labels.append(label)
            evidence.append(label)
    score = min(1.0, 0.2 * len(labels)) if labels else 0.0
    return {"has_signal": bool(labels), "label": labels[0] if labels else "unknown", "score": score, "evidence": evidence}


