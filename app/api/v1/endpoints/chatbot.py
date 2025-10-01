from typing import Any, Dict, List
import json
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import redis.asyncio as aioredis

from app.core.config import get_settings
from app.services.chroma_service import ChromaClientProvider, collection_name_for_os
from app.services.llm_service import _get_client, _get_ollama, SYSTEM
from app.api.v1.endpoints.alerts import list_alerts as list_alerts_endpoint

settings = get_settings()
redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
LOG = logging.getLogger(__name__)

router = APIRouter()

_provider: ChromaClientProvider | None = None


def _get_provider() -> ChromaClientProvider:
    global _provider
    if _provider is None:
        _provider = ChromaClientProvider()
    return _provider


class ChatRequest(BaseModel):
    message: str
    conversation_history: List[Dict[str, str]] | None = None


class ChatResponse(BaseModel):
    response: str
    sources: List[Dict[str, Any]]


def _generate_hyde_queries(user_query: str) -> List[str]:
    """Generate hypothetical document queries using HyDE technique."""
    system = """You are an expert at generating search queries. Given a user question about enterprise logs, 
generate 3 diverse search queries that would help find relevant information in a log analysis system.
Return ONLY a JSON array of strings, no other text."""
    
    prompt = f"""User question: {user_query}

Generate 3 search queries that would retrieve relevant logs, alerts, or incidents to answer this question.
Queries should be specific, technical, and cover different aspects of the question.
Return JSON array format: ["query1", "query2", "query3"]"""
    
    try:
        if settings.LLM_PROVIDER == "ollama":
            client = _get_ollama()
            resp = client.chat(
                model=settings.OLLAMA_CHAT_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                format="json",
            )
            content = (resp or {}).get("message", {}).get("content", "[]")
            result = json.loads(content)
        else:
            client = _get_client()
            response = client.chat.completions.create(
                model=settings.OPENAI_CHAT_MODEL,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ]
            )
            content = response.choices[0].message.content or "{}"
            result = json.loads(content)
        
        # Handle both {"queries": [...]} and bare array
        if isinstance(result, dict):
            queries = result.get("queries", [])
        elif isinstance(result, list):
            queries = result
        else:
            queries = []
            
        # Fallback to original query if generation fails
        if not queries:
            queries = [user_query]
            
        return [str(q) for q in queries[:3]]
    except Exception as e:
        LOG.warning("HyDE query generation failed: %s, using original query", e)
        return [user_query]


def _derive_severity_from_alert(alert: Dict[str, Any]) -> str:
    """Mirror UI severity mapping from failure_type into critical|warning|info."""
    failure_type = str(((alert.get("result") or {}).get("failure_type") or "")).lower()
    if not failure_type:
        return "info"
    if ("power" in failure_type) or ("raid" in failure_type) or ("storage" in failure_type):
        return "critical"
    if any(k in failure_type for k in [
        "disk", "nvme", "filesystem", "cpu", "memory", "network", "thermal"
    ]):
        return "warning"
    return "info"


def _decide_tool(user_query: str) -> Dict[str, Any]:
    """Ask LLM to decide if this query should call a backend tool (function).

    Returns a JSON dict like:
      {"action": "search_alerts", "params": {"severity": "critical", "limit": 3, "order": "desc"}}
    or {"action": "none"}
    """
    system = """You are a function-calling agent for an SRE chatbot. 
Analyze the user query and generate precise database query parameters.
Always respond with valid JSON only, no other text."""
    
    prompt = f"""
User query: "{user_query}"

Available tools:
1. search_alerts - Query alerts from database with filters

Parameters for search_alerts:
  - severity: "critical" | "warning" | "info" (if severity is mentioned or implied)
  - limit: integer (how many to return - extract numbers from "last N", "N alerts", etc.)
  - order: "desc" (always use desc for newest first)
  - os: "linux" | "macos" | "windows" | "unknown" (if OS mentioned)
  - failure_type_contains: string (if specific failure type like "disk", "network" mentioned)

Important extraction rules:
- "last critical alert" (singular) = limit:1
- "last 3 alerts", "last three alerts" = limit:3
- "critical" = severity:"critical"
- "warning" or "warnings" = severity:"warning"
- Always include order:"desc" when fetching alerts

Examples:
"last three critical alerts" → {{"action":"search_alerts","params":{{"severity":"critical","limit":3,"order":"desc"}}}}
"last critical alert" → {{"action":"search_alerts","params":{{"severity":"critical","limit":1,"order":"desc"}}}}
"show me 5 linux alerts" → {{"action":"search_alerts","params":{{"os":"linux","limit":5,"order":"desc"}}}}
"recent warning alerts" → {{"action":"search_alerts","params":{{"severity":"warning","limit":5,"order":"desc"}}}}

If query is NOT about fetching/filtering alerts, return: {{"action":"none"}}

Analyze and return JSON:
"""
    try:
        if settings.LLM_PROVIDER == "ollama":
            client = _get_ollama()
            resp = client.chat(
                model=settings.OLLAMA_CHAT_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                format="json",
            )
            text = (resp or {}).get("message", {}).get("content", "{}")
            return json.loads(text)
        else:
            client = _get_client()
            response = client.chat.completions.create(
                model=settings.OPENAI_CHAT_MODEL,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            content = response.choices[0].message.content or "{}"
            return json.loads(content)
    except Exception as e:
        LOG.debug("intent decide failed err=%s", e)
        return {"action": "none"}


async def _tool_search_alerts(params: Dict[str, Any]) -> Dict[str, Any]:
    """Implements the search_alerts tool using the same filtering logic as the UI where feasible."""
    # Fetch a reasonably large set and then filter in-memory similar to the UI
    alerts: List[Dict[str, Any]] = await list_alerts_endpoint(limit=500)

    # Apply filters
    severity = str(params.get("severity") or "").strip().lower()
    os_filter = str(params.get("os") or "").strip().lower()
    ft_contains = str(params.get("failure_type_contains") or "").strip().lower()
    order = (params.get("order") or "desc").strip().lower()
    limit = int(params.get("limit") or 5)

    filtered: List[Dict[str, Any]] = []
    for a in alerts:
        # severity
        if severity:
            sev = _derive_severity_from_alert(a)
            if sev != severity:
                continue
        # OS
        if os_filter and (str(a.get("os", "")).lower() != os_filter):
            continue
        # failure_type contains
        if ft_contains:
            ft_val = str(((a.get("result") or {}).get("failure_type") or "")).lower()
            if ft_contains not in ft_val:
                continue
        filtered.append(a)

    # Sort by stream id (which encodes time) desc by default
    filtered.sort(key=lambda x: x.get("id", ""), reverse=(order != "asc"))
    items = filtered[:limit]

    if not items:
        msg = f"I couldn't find any alerts matching the requested filters."
        return {"text": msg, "sources": []}

    # Generate technical assistant response with markdown and guidance
    msg = _generate_alert_guidance(items, severity, limit)

    sources = [{
        "type": "alert",
        "id": item.get("id"),
        "os": item.get("os"),
        "summary": item.get("summary") or ((item.get("result") or {}).get("summary") or ""),
    } for item in items]

    return {"text": msg, "sources": sources}


def _generate_alert_guidance(items: List[Dict[str, Any]], severity: str, limit: int) -> str:
    """Generate markdown-formatted response with technical guidance and solutions."""
    if not items:
        return "No alerts found matching your criteria."
    
    # Build markdown response
    lines = []
    
    # Header with context
    count = len(items)
    sev_label = severity.upper() if severity else "ALERT"
    if severity == "critical":
        lines.append(f"## ⚠️ {count} Critical Alert{'s' if count > 1 else ''} Found\n")
        lines.append("**Immediate attention required.** These alerts indicate high-risk conditions that may lead to outages or data loss.\n")
    elif severity == "warning":
        lines.append(f"## ⚡ {count} Warning Alert{'s' if count > 1 else ''} Found\n")
        lines.append("**Action recommended.** These alerts indicate degraded performance or impending failures.\n")
    else:
        lines.append(f"## 📋 {count} Alert{'s' if count > 1 else ''} Found\n")
    
    # List alerts with details
    for idx, item in enumerate(items, start=1):
        result = item.get("result") or {}
        summary = item.get("summary") or result.get("summary") or "Unknown issue"
        os_name = item.get("os") or "unknown"
        failure_type = result.get("failure_type") or "unknown"
        confidence = result.get("confidence")
        recommendation = item.get("solution") or result.get("recommendation") or ""
        
        conf_pct = ""
        if isinstance(confidence, (int, float)):
            conf_pct = f" ({int(confidence * 100)}% confidence)"
        
        lines.append(f"### {idx}. {summary}")
        lines.append(f"**OS:** {os_name.upper()} | **Type:** `{failure_type}`{conf_pct}\n")
        
        if recommendation:
            lines.append(f"**Recommended Action:**")
            lines.append(f"> {recommendation}\n")
    
    # Add guidance section for smaller result sets
    if limit <= 3 and items:
        lines.append("---\n")
        lines.append("### 💡 Technical Guidance\n")
        
        # Generate context-specific guidance using LLM
        guidance = _generate_contextual_guidance(items, severity)
        lines.append(guidance)
        
        lines.append("\n**Next Steps:**")
        if severity == "critical":
            lines.append("1. Review the alerts immediately and assess impact")
            lines.append("2. Execute recommended actions to mitigate risks")
            lines.append("3. Check the **Incidents** tab for related patterns")
            lines.append("4. Consider persisting critical alerts for tracking")
        elif severity == "warning":
            lines.append("1. Plan remediation during your next maintenance window")
            lines.append("2. Monitor for escalation to critical status")
            lines.append("3. Review historical trends in **Analytics**")
        else:
            lines.append("1. Review alerts and determine if action is needed")
            lines.append("2. Use filters in the **Alerts** tab to refine your view")
            lines.append("3. Check **Fleet** view for system-wide patterns")
    
    return "\n".join(lines)


def _generate_contextual_guidance(items: List[Dict[str, Any]], severity: str) -> str:
    """Use LLM to generate contextual technical guidance based on alerts."""
    if not items:
        return "No specific guidance available."
    
    # Build context from alerts
    alert_summaries = []
    for item in items[:3]:  # Limit to first 3 for context
        result = item.get("result") or {}
        summary = item.get("summary") or result.get("summary") or ""
        failure_type = result.get("failure_type") or ""
        os_name = item.get("os") or ""
        alert_summaries.append(f"- {os_name}: {failure_type} - {summary}")
    
    context = "\n".join(alert_summaries)
    
    system = "You are an expert SRE providing concise technical guidance to operators. Be brief and actionable."
    prompt = f"""Provide 2-3 sentences of technical guidance for an operator dealing with these alerts:

{context}

Focus on:
- Root cause patterns
- Preventive measures
- Monitoring recommendations

Keep it concise, technical, and actionable."""
    
    try:
        if settings.LLM_PROVIDER == "ollama":
            client = _get_ollama()
            resp = client.chat(
                model=settings.OLLAMA_CHAT_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            return (resp or {}).get("message", {}).get("content", "Monitor these systems closely and follow the recommended actions above.")
        else:
            client = _get_client()
            response = client.chat.completions.create(
                model=settings.OPENAI_CHAT_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            return response.choices[0].message.content or "Monitor these systems closely and follow the recommended actions above."
    except Exception as e:
        LOG.debug("Guidance generation failed: %s", e)
        return "Monitor these systems closely and follow the recommended actions above."


async def _search_alerts(queries: List[str], limit: int = 10) -> List[Dict[str, Any]]:
    """Search recent alerts using queries."""
    alerts = []
    try:
        # Get recent alerts from Redis
        stream_entries = await redis.xrevrange(settings.ALERTS_STREAM, max="+", min="-", count=100)
        
        for entry_id, fields in stream_entries:
            try:
                result_obj = {}
                if "result" in fields:
                    result_obj = json.loads(fields.get("result", "{}"))
                
                summary = fields.get("summary", "") or result_obj.get("summary", "")
                solution = fields.get("solution", "") or result_obj.get("recommendation", "")
                failure_type = result_obj.get("failure_type", "")
                
                # Create searchable text
                searchable = f"{summary} {solution} {failure_type} {fields.get('os', '')}".lower()
                
                # Simple keyword matching (could be enhanced with embeddings)
                relevance = 0
                for query in queries:
                    query_lower = query.lower()
                    if query_lower in searchable:
                        relevance += 1
                    # Check individual words
                    for word in query_lower.split():
                        if len(word) > 3 and word in searchable:
                            relevance += 0.5
                
                if relevance > 0:
                    alerts.append({
                        "id": entry_id,
                        "type": fields.get("type", "alert"),
                        "os": fields.get("os", ""),
                        "summary": summary,
                        "solution": solution,
                        "issue_key": fields.get("issue_key", ""),
                        "relevance": relevance,
                        "result": result_obj
                    })
            except Exception as e:
                LOG.debug("Error processing alert %s: %s", entry_id, e)
                continue
        
        # Sort by relevance and return top results
        alerts.sort(key=lambda x: x["relevance"], reverse=True)
        return alerts[:limit]
    except Exception as e:
        LOG.error("Error searching alerts: %s", e)
        return []


async def _search_incidents(queries: List[str], limit: int = 10) -> List[Dict[str, Any]]:
    """Search recent incidents using queries."""
    incidents = []
    try:
        # Get recent incidents from Redis
        entries = await redis.xrevrange(settings.ISSUES_CANDIDATES_STREAM, max="+", min="-", count=100)
        
        for entry_id, fields in entries:
            try:
                templated_summary = fields.get("templated_summary", "")
                os_name = fields.get("os", "")
                issue_key = fields.get("issue_key", "")
                
                # Create searchable text
                searchable = f"{templated_summary} {os_name} {issue_key}".lower()
                
                # Simple keyword matching
                relevance = 0
                for query in queries:
                    query_lower = query.lower()
                    if query_lower in searchable:
                        relevance += 1
                    for word in query_lower.split():
                        if len(word) > 3 and word in searchable:
                            relevance += 0.5
                
                if relevance > 0:
                    incidents.append({
                        "id": entry_id,
                        "type": "incident",
                        "os": os_name,
                        "issue_key": issue_key,
                        "templated_summary": templated_summary[:500],  # Limit length
                        "relevance": relevance
                    })
            except Exception as e:
                LOG.debug("Error processing incident %s: %s", entry_id, e)
                continue
        
        incidents.sort(key=lambda x: x["relevance"], reverse=True)
        return incidents[:limit]
    except Exception as e:
        LOG.error("Error searching incidents: %s", e)
        return []


def _search_vector_db(queries: List[str], os_list: List[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    """Search ChromaDB vector store for relevant logs using HyDE queries."""
    results = []
    
    if os_list is None:
        os_list = ["linux", "macos", "windows"]
    
    try:
        provider = _get_provider()
        
        for os_name in os_list:
            try:
                collection_name = collection_name_for_os(os_name)
                collection = provider.get_or_create_collection(collection_name)
                
                # Search with each query
                for query in queries:
                    try:
                        search_results = collection.query(
                            query_texts=[query],
                            n_results=min(limit, 5),
                            include=["documents", "metadatas", "distances"]
                        )
                        
                        if search_results and search_results.get("documents"):
                            for i, doc in enumerate(search_results["documents"][0]):
                                distance = search_results["distances"][0][i] if search_results.get("distances") else 1.0
                                metadata = search_results["metadatas"][0][i] if search_results.get("metadatas") else {}
                                
                                results.append({
                                    "type": "log_template",
                                    "os": os_name,
                                    "document": doc[:500],  # Limit length
                                    "distance": distance,
                                    "metadata": metadata
                                })
                    except Exception as e:
                        LOG.debug("Error querying collection %s: %s", collection_name, e)
                        continue
            except Exception as e:
                LOG.debug("Error with OS %s: %s", os_name, e)
                continue
        
        # Sort by distance (lower is better) and deduplicate
        results.sort(key=lambda x: x.get("distance", 1.0))
        
        # Simple deduplication by document content
        seen = set()
        unique_results = []
        for r in results:
            doc_hash = hash(r.get("document", ""))
            if doc_hash not in seen:
                seen.add(doc_hash)
                unique_results.append(r)
        
        return unique_results[:limit]
    except Exception as e:
        LOG.error("Error searching vector DB: %s", e)
        return []


def _synthesize_response(user_query: str, alerts: List[Dict], incidents: List[Dict], logs: List[Dict]) -> str:
    """Use LLM to synthesize a helpful response from retrieved context."""
    # Build context from sources
    context_parts = []
    
    if alerts:
        context_parts.append("## Recent Alerts:")
        for alert in alerts[:5]:
            context_parts.append(f"- [{alert.get('os', 'unknown')}] {alert.get('summary', 'N/A')}")
            if alert.get('solution'):
                context_parts.append(f"  Solution: {alert['solution']}")
    
    if incidents:
        context_parts.append("\n## Recent Incidents:")
        for incident in incidents[:5]:
            context_parts.append(f"- [{incident.get('os', 'unknown')}] {incident.get('templated_summary', 'N/A')[:200]}")
    
    if logs:
        context_parts.append("\n## Related Log Templates:")
        for log in logs[:5]:
            context_parts.append(f"- [{log.get('os', 'unknown')}] {log.get('document', 'N/A')[:200]}")
    
    context = "\n".join(context_parts) if context_parts else "No relevant information found in the system."
    
    system_prompt = """You are an expert SRE assistant for an enterprise log analysis system. 
Your job is to help users understand their logs, alerts, and incidents.
Answer questions based ONLY on the provided context from the log analysis system.
Be concise, technical, and helpful. If the context doesn't contain relevant information, say so clearly.
Format your response in a friendly, conversational manner."""
    
    user_prompt = f"""User Question: {user_query}

Context from the log analysis system:
{context}

Provide a helpful, concise answer based on the context above. If you can give specific recommendations or insights, please do."""
    
    try:
        if settings.LLM_PROVIDER == "ollama":
            client = _get_ollama()
            resp = client.chat(
                model=settings.OLLAMA_CHAT_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return (resp or {}).get("message", {}).get("content", "I couldn't generate a response at this time.")
        else:
            client = _get_client()
            response = client.chat.completions.create(
                model=settings.OPENAI_CHAT_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return response.choices[0].message.content or "I couldn't generate a response at this time."
    except Exception as e:
        LOG.error("Error synthesizing response: %s", e)
        return f"I found some relevant information but encountered an error generating a response. Please check the sources below."


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Chat endpoint that uses HyDE RAG over existing issues, alerts, and logs."""
    user_query = request.message.strip()
    
    if not user_query:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    
    LOG.info("Chat query: %s", user_query)
    
    # LLM function calling to decide tool and generate query parameters
    tool_decision = _decide_tool(user_query) or {"action": "none"}
    action = str(tool_decision.get("action") or "none").lower()
    
    LOG.info("Tool decision: action=%s params=%s", action, tool_decision.get("params"))
    
    if action == "search_alerts":
        tool_result = await _tool_search_alerts(tool_decision.get("params") or {})
        response_text = tool_result.get("text", "") or "No alerts found."
        tool_sources = tool_result.get("sources") or []

        return ChatResponse(response=response_text, sources=tool_sources)

    # Fallback: HyDE RAG generic QA
    hyde_queries = _generate_hyde_queries(user_query)
    LOG.debug("Generated HyDE queries: %s", hyde_queries)

    alerts = await _search_alerts(hyde_queries, limit=10)
    incidents = await _search_incidents(hyde_queries, limit=10)
    logs = _search_vector_db(hyde_queries, limit=10)

    LOG.debug("Found %d alerts, %d incidents, %d logs", len(alerts), len(incidents), len(logs))

    response_text = _synthesize_response(user_query, alerts, incidents, logs)
    
    # Step 4: Compile sources for transparency
    all_sources = []
    
    for alert in alerts[:5]:
        all_sources.append({
            "type": "alert",
            "id": alert.get("id", ""),
            "os": alert.get("os", ""),
            "summary": alert.get("summary", ""),
            "relevance": alert.get("relevance", 0)
        })
    
    for incident in incidents[:5]:
        all_sources.append({
            "type": "incident",
            "id": incident.get("id", ""),
            "os": incident.get("os", ""),
            "summary": incident.get("templated_summary", "")[:200],
            "relevance": incident.get("relevance", 0)
        })
    
    for log in logs[:5]:
        all_sources.append({
            "type": "log",
            "os": log.get("os", ""),
            "document": log.get("document", "")[:200],
            "distance": log.get("distance", 1.0)
        })
    
    return ChatResponse(response=response_text, sources=all_sources)


@router.get("/health")
async def chatbot_health() -> Dict[str, Any]:
    """Check if chatbot services are available."""
    try:
        # Check Redis
        await redis.ping()
        redis_ok = True
    except Exception as e:
        redis_ok = False
        LOG.error("Redis health check failed: %s", e)
    
    # Check ChromaDB
    try:
        provider = _get_provider()
        provider.client.heartbeat()
        chroma_ok = True
    except Exception as e:
        chroma_ok = False
        LOG.error("ChromaDB health check failed: %s", e)
    
    # Check LLM
    llm_ok = True
    try:
        from app.services.llm_service import llm_healthcheck
        llm_result = llm_healthcheck()
        llm_ok = llm_result.get("ok", False)
    except Exception as e:
        llm_ok = False
        LOG.error("LLM health check failed: %s", e)
    
    overall_ok = redis_ok and chroma_ok and llm_ok
    
    return {
        "status": "ok" if overall_ok else "degraded",
        "redis": redis_ok,
        "chromadb": chroma_ok,
        "llm": llm_ok
    }

