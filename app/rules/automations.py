from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
import yaml


_cache: Dict[str, Any] | None = None


def load_rules() -> Dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    path = Path(__file__).with_name("automations.yml")
    if not path.exists():
        _cache = {"rules": []}
        return _cache
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        data = {"rules": []}
    if "rules" not in data or not isinstance(data["rules"], list):
        data["rules"] = []
    _cache = data
    return data


def _rules_path() -> Path:
    return Path(__file__).with_name("automations.yml")


def get_rules() -> List[Dict[str, Any]]:
    return list(load_rules().get("rules") or [])


def save_rules(data: Dict[str, Any]) -> None:
    global _cache
    path = _rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    _cache = None  # drop cache to force reload


def upsert_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    data = load_rules()
    rules = list(data.get("rules") or [])
    rid = str(rule.get("id") or "").strip()
    if not rid:
        raise ValueError("rule.id is required")
    replaced = False
    for i, r in enumerate(rules):
        if str(r.get("id")) == rid:
            rules[i] = rule
            replaced = True
            break
    if not replaced:
        rules.append(rule)
    data["rules"] = rules
    save_rules(data)
    return rule


def delete_rule(rule_id: str) -> bool:
    data = load_rules()
    rules = list(data.get("rules") or [])
    new_rules = [r for r in rules if str(r.get("id")) != str(rule_id)]
    if len(new_rules) == len(rules):
        return False
    data["rules"] = new_rules
    save_rules(data)
    return True


