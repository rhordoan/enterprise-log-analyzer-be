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


