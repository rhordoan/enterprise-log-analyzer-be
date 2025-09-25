from __future__ import annotations

from typing import Any, Dict, List
import logging
import json

from openai import OpenAI
import ollama

from app.core.config import settings


LOG = logging.getLogger(__name__)


_client: OpenAI | None = None
_ollama_client: ollama.Client | None = None


def _get_client() -> OpenAI:
    """Initializes and returns a singleton OpenAI client."""
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=settings.OPENAI_API_KEY,
        )
    return _client


def _get_ollama() -> ollama.Client:
    """Initializes and returns a singleton Ollama client."""
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = ollama.Client(host=settings.OLLAMA_BASE_URL)
    return _ollama_client


def _chat_json_with_openai(system: str, user_prompt: str) -> Dict[str, Any]:
    """
    Sends a chat request to OpenAI, ensuring a JSON object is returned.
    Improved with simplified parsing and more robust error handling.
    """
    client = _get_client()
    try:
        response = client.chat.completions.create(
            model=settings.OPENAI_CHAT_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ]
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)
    except Exception as e:
        # Fallback for API errors or other issues
        LOG.error("LLM(OpenAI) chat failed model=%s err=%s", settings.OPENAI_CHAT_MODEL, e)
        return {"raw": str(e), "error": "OpenAI API call failed."}


def _chat_json_with_ollama(system: str, user_prompt: str, temperature: float) -> Dict[str, Any]:
    """
    Sends a chat request to Ollama, ensuring a JSON object is returned.
    Improved to enforce JSON format via the API and prevent NameError on exceptions.
    """
    client = _get_ollama()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    resp = None  # Initialize resp to prevent NameError in the except block
    try:
        # Enforce JSON format directly in the API call for reliability
        resp = client.chat(
            model=settings.OLLAMA_CHAT_MODEL,
            messages=messages,
            format="json",  # <-- Key improvement: Enforces JSON output
            options={"temperature": temperature}
        )
        message = (resp or {}).get("message", {})
        text = message.get("content", "{}")
        return json.loads(text)
    except Exception as e:
        # Fallback is now safe from NameError
        text = ""
        if resp and isinstance(resp, dict):
            text = resp.get("message", {}).get("content", "")
        else:
            text = str(e) # The error was likely in the API call itself
        LOG.error("LLM(Ollama) chat failed model=%s err=%s", settings.OLLAMA_CHAT_MODEL, e)
        return {"raw": text, "error": "Failed to get or parse Ollama response."}


SYSTEM = "You are an SRE assistant. Respond ONLY with valid JSON."


def classify_failure(os_name: str, raw: str, templated: str, neighbors: List[Dict[str, Any]]) -> Dict[str, Any]:
    """LLM-based classification for hardware failure likelihood with structured JSON output."""
    examples = "\n".join([f"- {n.get('document', '')}" for n in neighbors[:5]])
    # Expanded failure type taxonomy to capture a broader set of common incident categories.
    # Keep this list in sync with rule labels in app/rules/rules.yml when practical.
    # Note: Use lowercase, single tokens where possible for stability.
    failure_types = "|".join([
        "disk", "storage", "raid", "nvme", "filesystem", "io",
        "cpu", "memory", "network", "power", "thermal", "wifi",
        "windows_update", "service_failure", "sandbox",
        "application", "configuration", "security", "dependency",
        "kernel", "driver", "os_update", "unknown"
    ])
    prompt = f"""
OS: {os_name}
Current log (templated): {templated}
Current log (raw): {raw}
Similar known templates/logs:
{examples}

Return JSON with:
{{
  "is_hardware_failure": true|false,
  "failure_type": "{failure_types}",
  "confidence": 0..1,
  "evidence": ["..."],
  "recommendation": "..."
}}
Only JSON; no extra text.
"""
    if settings.LLM_PROVIDER == "ollama":
        return _chat_json_with_ollama(SYSTEM, prompt, temperature=0.1)
    else:
        return _chat_json_with_openai(SYSTEM, prompt)


def generate_hypothesis(os_name: str, templated_summary: str, top_logs: List[Dict[str, Any]], num_queries: int = 3) -> List[str]:
    """Generate HYDE-style retrieval hypotheses/queries from an issue summary and logs."""
    logs_snippets = "\n".join([f"- {item.get('templated','')}" for item in top_logs[:20]])
    prompt = f"""
OS: {os_name}
Issue summary (templated):
{templated_summary}

Key logs (templated):
{logs_snippets}

Write {num_queries} short search queries (max 12 words each) that would retrieve additional logs relevant to diagnosing this issue. Return JSON list of strings only.
"""
    if settings.LLM_PROVIDER == "ollama":
        result = _chat_json_with_ollama(SYSTEM, prompt, temperature=0.2)
    else:
        result = _chat_json_with_openai(SYSTEM, prompt)
        
    # Accept either {"queries": [...]} or a bare list
    if isinstance(result, dict):
        queries = result.get("queries") if isinstance(result.get("queries"), list) else None
        if queries:
            return [str(q) for q in queries][:num_queries]
    if isinstance(result, list):
        return [str(q) for q in result][:num_queries]
    return []


def classify_issue(os_name: str, top_logs: List[Dict[str, Any]], neighbors: List[Dict[str, Any]], retrieved_logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """LLM-based classification for an aggregated issue."""
    examples = "\n".join([f"- {n.get('document', '')}" for n in neighbors[:8]])
    recent = "\n".join([f"- {l.get('templated','')}" for l in top_logs[:50]])
    extra = "\n".join([f"- {l.get('templated','')}" for l in retrieved_logs[:20]])
    # Expanded failure type taxonomy (keep aligned with classify_failure)
    failure_types = "|".join([
        "disk", "storage", "raid", "nvme", "filesystem", "io",
        "cpu", "memory", "network", "power", "thermal", "wifi",
        "windows_update", "service_failure", "sandbox",
        "application", "configuration", "security", "dependency",
        "kernel", "driver", "os_update", "unknown"
    ])
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
  "failure_type": "{failure_types}",
  "confidence": 0..1,
  "top_signals": ["..."],
  "summary": "...",
  "recommendation": "..."
}}
Only JSON; no extra text.
"""
    if settings.LLM_PROVIDER == "ollama":
        return _chat_json_with_ollama(SYSTEM, prompt, temperature=0.3)
    else:
        return _chat_json_with_openai(SYSTEM, prompt)


def llm_healthcheck() -> Dict[str, Any]:
    """Attempt a minimal LLM call to verify availability; logs success/failure.

    Returns a dict like {"ok": bool, "provider": str, "model": str, "error": str|None}
    """
    system = "You are a healthcheck. Respond with JSON."
    prompt = "Return {\"ok\": true} as valid JSON only."
    try:
        if settings.LLM_PROVIDER == "ollama":
            res = _chat_json_with_ollama(system, prompt, temperature=0.0)
            ok = isinstance(res, dict) and bool(res.get("ok") is True)
            if ok:
                LOG.info("LLM health ok provider=ollama model=%s", settings.OLLAMA_CHAT_MODEL)
                return {"ok": True, "provider": "ollama", "model": settings.OLLAMA_CHAT_MODEL}
            LOG.error("LLM health unexpected response provider=ollama model=%s resp=%s", settings.OLLAMA_CHAT_MODEL, res)
            return {"ok": False, "provider": "ollama", "model": settings.OLLAMA_CHAT_MODEL, "error": "unexpected_response"}
        else:
            res = _chat_json_with_openai(system, prompt)
            ok = isinstance(res, dict) and bool(res.get("ok") is True)
            if ok:
                LOG.info("LLM health ok provider=openai model=%s", settings.OPENAI_CHAT_MODEL)
                return {"ok": True, "provider": "openai", "model": settings.OPENAI_CHAT_MODEL}
            LOG.error("LLM health unexpected response provider=openai model=%s resp=%s", settings.OPENAI_CHAT_MODEL, res)
            return {"ok": False, "provider": "openai", "model": settings.OPENAI_CHAT_MODEL, "error": "unexpected_response"}
    except Exception as e:
        LOG.error("LLM health failed provider=%s err=%s", settings.LLM_PROVIDER, e)
        return {"ok": False, "provider": settings.LLM_PROVIDER, "model": (settings.OLLAMA_CHAT_MODEL if settings.LLM_PROVIDER=="ollama" else settings.OPENAI_CHAT_MODEL), "error": str(e)}