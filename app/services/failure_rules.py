from __future__ import annotations

import re
from typing import Dict, List, Tuple
import yaml
from pathlib import Path


_RULES_FILE = Path(__file__).parent.parent / "rules" / "rules.yml"


def load_rules() -> List[Tuple[str, re.Pattern[str]]]:
    """Load rules from the YAML file."""
    if not _RULES_FILE.exists():
        return []
    with open(_RULES_FILE, "r") as f:
        data = yaml.safe_load(f)

    rules = []
    for rule in data.get("rules", []):
        rules.append(
            (
                rule["name"],
                re.compile(rule["pattern"], re.IGNORECASE),
            )
        )
    return rules

_RULES = load_rules()

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


