from __future__ import annotations

from typing import Any, Dict, List

from openai import OpenAI

from app.core.config import settings


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


SYSTEM = "You are an SRE assistant. Respond ONLY with valid JSON."


def classify_failure(os_name: str, raw: str, templated: str, neighbors: List[Dict[str, Any]]) -> Dict[str, Any]:
    """LLM-based classification for hardware failure likelihood with structured JSON output."""
    examples = "\n".join([f"- {n.get('document', '')}" for n in neighbors[:5]])
    prompt = f"""
OS: {os_name}
Current log (templated): {templated}
Current log (raw): {raw}
Similar known templates/logs:
{examples}

Return JSON with:
{{
  "is_hardware_failure": true|false,
  "failure_type": "disk|memory|cpu|io|network|power|unknown",
  "confidence": 0..1,
  "evidence": ["..."],
  "recommendation": "..."
}}
Only JSON; no extra text.
"""
    client = _get_client()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    content = response.choices[0].message
    # SDK v1 returns parsed JSON when response_format is set; fall back to content
    parsed = getattr(content, "parsed", None)
    return parsed if isinstance(parsed, dict) else {"raw": getattr(content, "content", "")}



def generate_hypothesis(os_name: str, templated_summary: str, top_logs: List[Dict[str, Any]], num_queries: int = 3) -> List[str]:
    """Generate HYDE-style retrieval hypotheses/queries from an issue summary and logs.

    Returns a small list of short queries to use for vector retrieval.
    """
    logs_snippets = "\n".join([f"- {item.get('templated','')}" for item in top_logs[:20]])
    prompt = f"""
OS: {os_name}
Issue summary (templated):
{templated_summary}

Key logs (templated):
{logs_snippets}

Write {num_queries} short search queries (max 12 words each) that would retrieve additional logs relevant to diagnosing this issue. Return JSON list of strings only.
"""
    client = _get_client()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    content = response.choices[0].message
    parsed = getattr(content, "parsed", None)
    if isinstance(parsed, dict):
        # allow either {"queries": [...]} or a bare list encoded as JSON
        queries = parsed.get("queries") if isinstance(parsed.get("queries"), list) else None
        if queries:
            return [str(q) for q in queries][:num_queries]
    # fallback: try to parse plain content as a simple JSON array-like string
    text = getattr(content, "content", "") or "[]"
    try:
        import json
        arr = json.loads(text)
        if isinstance(arr, list):
            return [str(q) for q in arr][:num_queries]
    except Exception:
        pass
    return []


def classify_issue(os_name: str, top_logs: List[Dict[str, Any]], neighbors: List[Dict[str, Any]], retrieved_logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """LLM-based classification for an aggregated issue.

    Input:
    - top_logs: list of {templated, raw}
    - neighbors: nearest known templates/logs from templates_<os>
    - retrieved_logs: HYDE retrieved additional logs from logs_<os>
    """
    examples = "\n".join([f"- {n.get('document', '')}" for n in neighbors[:8]])
    recent = "\n".join([f"- {l.get('templated','')}" for l in top_logs[:50]])
    extra = "\n".join([f"- {l.get('templated','')}" for l in retrieved_logs[:20]])
    prompt = f"""
OS: {os_name}
Issue logs (templated):
{recent}

Similar known templates/logs:
{examples}

Additional retrieved logs:
{extra}

Return JSON with:
{{
  "is_hardware_failure": true|false,
  "failure_type": "disk|memory|cpu|io|network|power|unknown",
  "confidence": 0..1,
  "top_signals": ["..."],
  "summary": "...",
  "recommendation": "..."
}}
Only JSON; no extra text.
"""
    client = _get_client()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )
    content = response.choices[0].message
    parsed = getattr(content, "parsed", None)
    return parsed if isinstance(parsed, dict) else {"raw": getattr(content, "content", "")}
