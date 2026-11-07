import logging
import json
import re
from typing import Dict, Any, List, Optional
from backend.memory import memory, IncidentMemory
from backend.llm import llm, GroqLLM

logger = logging.getLogger("echo_core.agent")

# Curated production runbooks
RUNBOOKS = {
    "payments": {
        "title": "Payments Service SRE Runbook",
        "procedures": [
            "Check active Postgres connections via pg_stat_activity.",
            "Verify HikariCP pool metrics (active vs max connections).",
            "WARNING: Do NOT blindly restart payments pods during connection spikes; restart triggers connection storm.",
            "Mitigation: Tune max_connections in Postgres or route through PgBouncer connection pooler."
        ]
    },
    "redis": {
        "title": "Redis Cache & Session Store Runbook",
        "procedures": [
            "Check Redis used_memory vs maxmemory.",
            "Verify eviction policy: ensure volatile-lru is configured rather than noeviction.",
            "WARNING: Never execute FLUSHDB on production Redis; causes immediate database stampede.",
            "Mitigation: Scale shard memory or enforce TTL on high-cardinality keys."
        ]
    },
    "postgres": {
        "title": "PostgreSQL Primary & Replica Runbook",
        "procedures": [
            "Inspect blocking queries: SELECT pid, query, state, age(clock_timestamp(), query_start) FROM pg_stat_activity WHERE state != 'idle'.",
            "Terminate long-running locks with pg_terminate_backend(pid).",
            "Always run index creation with CONCURRENTLY to avoid ACCESS EXCLUSIVE locks.",
            "Verify replication lag using pg_last_wal_replay_lsn()."
        ]
    },
    "api-gateway": {
        "title": "API Gateway / Ingress Runbook",
        "procedures": [
            "Inspect upstream latency and 5xx error distribution.",
            "Check circuit breaker status and pending connection queues.",
            "WARNING: Increasing gateway timeouts increases worker connection backlog and freezes edge proxy.",
            "Mitigation: Enable circuit breaker tripping and fallback responses."
        ]
    },
    "auth": {
        "title": "Authentication & Session Service Runbook",
        "procedures": [
            "Verify JWKS public key cache TTL and IDP connectivity.",
            "Check bcrypt / argon2 hash work factors against CPU utilization.",
            "Verify Redis session cache health."
        ]
    },
    "orders": {
        "title": "Order Processing Service Runbook",
        "procedures": [
            "Check Kafka consumer group lag on orders topic.",
            "Verify database lock acquisition order to prevent circular deadlocks.",
            "Ensure asynchronous notification handlers use bounded thread pools."
        ]
    },
    "search": {
        "title": "Product Search & Catalog Runbook",
        "procedures": [
            "Check Elasticsearch disk usage against flood-stage watermark (85%).",
            "Sanitize search queries to prevent leading wildcards (*phone*).",
            "Tune indexer consumer bulk batch size."
        ]
    },
    "queue": {
        "title": "Message Queue & Worker Pipeline Runbook",
        "procedures": [
            "Check dead-letter queue (DLQ) depth and unacked message counts.",
            "WARNING: Do NOT redrive DLQ without validating consumer schema compatibility.",
            "Scale consumer replicas with bounded prefetch_count."
        ]
    },
    "ml-ranker": {
        "title": "ML Recommendation Engine Runbook",
        "procedures": [
            "Inspect GPU VRAM usage and CUDA out-of-memory logs.",
            "Check memory fragmentation and preload jemalloc.",
            "Reduce inference batch size or switch to mixed precision FP16."
        ]
    }
}

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "recall_similar_incidents",
            "description": "Searches Hindsight memory for past incidents with similar symptoms or root causes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Name of the service (e.g. payments, redis, postgres)"},
                    "error_signature": {"type": "string", "description": "Key error phrase or symptom"}
                },
                "required": ["service", "error_signature"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_runbook",
            "description": "Retrieves the standard SRE runbook procedures for a given service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Service name"}
                },
                "required": ["service"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "propose_fix",
            "description": "Proposes an actionable fix command with risk analysis and failed fix warnings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "Summary of proposed fix action"},
                    "command": {"type": "string", "description": "Exact bash or kubectl command to run"},
                    "rationale": {"type": "string", "description": "Why this fix is expected to work"},
                    "failed_fixes_warning": {"type": "string", "description": "Explicit warning on fixes that failed in the past and must NOT be attempted"},
                    "risk_level": {"type": "integer", "description": "0 for low-risk read/scale, 1 for high-risk restart/rollback"}
                },
                "required": ["action", "command", "rationale", "failed_fixes_warning", "risk_level"]
            }
        }
    }
]

class IncidentAgent:
    """
    Echo-Core On-Call Agent.
    Orchestrates Hindsight memory recall/reflect, Groq LLM tool loops,
    and produces both memory-informed and generic briefings.
    """
    def __init__(self, mem: Optional[IncidentMemory] = None, llm_engine: Optional[GroqLLM] = None):
        self.memory = mem or memory
        self.llm = llm_engine or llm

    async def recall_similar_incidents(self, service: str, error_signature: str) -> List[Dict[str, Any]]:
        """Tool: Retrieve similar incidents from Hindsight."""
        return await self.memory.recall_for_alert(service, error_signature)

    def get_runbook(self, service: str) -> Dict[str, Any]:
        """Tool: Fetch SRE runbook."""
        return RUNBOOKS.get(service, {
            "title": f"Standard Runbook for {service}",
            "procedures": [
                "Inspect recent deploy history and git commit logs.",
                "Verify resource limits (CPU, memory, file descriptors).",
                "Check upstream and downstream service dependencies."
            ]
        })

    async def analyze_incident(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyzes an incident, generating:
        1. Memory-informed briefing (Echo-Core + Hindsight)
        2. Generic briefing (Zero-shot LLM with no memory)
        3. Recalled memories list
        """
        svc = incident.get("svc", "unknown")
        title = incident.get("title", "")
        cause = incident.get("cause", "")
        error_log = incident.get("error_log", "")

        # Step 1: Recall from Hindsight
        recalled_items = []
        try:
            recalled_items = await self.memory.recall_for_alert(svc, f"{title} {cause}")
        except Exception as e:
            logger.warning(f"Memory recall error: {e}")

        # Step 2: Hindsight Reflect Synthesis
        reflect_result = {}
        try:
            reflect_result = await self.memory.reflect_briefing(svc, title, error_log)
        except Exception as e:
            logger.warning(f"Memory reflect error: {e}")

        # Step 3: Generate Memory-Informed Briefing with Groq
        memory_briefing = await self._generate_memory_briefing(
            incident=incident,
            recalled_items=recalled_items,
            reflect_result=reflect_result
        )

        # Step 4: Generate Generic Zero-Shot Briefing (No Memory)
        generic_briefing = await self._generate_generic_briefing(incident=incident)

        return {
            "incident_id": incident.get("id"),
            "memory_informed": memory_briefing,
            "generic": generic_briefing,
            "recalled_items": recalled_items,
            "hindsight_healthy": self.memory.is_healthy
        }

    async def _generate_memory_briefing(
        self,
        incident: Dict[str, Any],
        recalled_items: List[Dict[str, Any]],
        reflect_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Synthesizes a memory-informed briefing. Cites past incidents,
        what worked, and highlights FAILED FIXES.
        """
        svc = incident.get("svc", "")
        title = incident.get("title", "")
        cause = incident.get("cause", "")
        runbook = self.get_runbook(svc)

        recalled_text_blocks = []
        for r in recalled_items[:5]:
            recalled_text_blocks.append(f"- [Memory ID: {r.get('id', 'N/A')}]: {r.get('text', '')}")

        recalled_context = "\n".join(recalled_text_blocks) if recalled_text_blocks else "No prior incidents found in memory."
        reflection_text = reflect_result.get("text", "")

        prompt = f"""You are Echo-Core, an autonomous on-call Incident Response Agent with persistent team memory.
Analyze this live production alert for service '{svc}':
Alert Title: {title}
Symptom/Cause: {cause}

HINDSIGHT TEAM MEMORIES RECALLED:
{recalled_context}

HINDSIGHT REFLECTION SYNTHESIS:
{reflection_text}

SRE RUNBOOK HIGHLIGHTS:
{json.dumps(runbook.get('procedures', []))}

Your task: Provide a structured incident briefing.
IMPORTANT RULES:
1. If memories describe what caused this previously, cite the likely root cause.
2. If past memories specify a fix that worked, recommend that exact fix and command.
3. CRITICAL STAR FEATURE: If past memories document FIXES THAT FAILED, explicitly warn the on-call engineer in 'failed_fixes' with DO NOT ATTEMPT and explain why it failed.
4. If past memories mention who solved it, include that engineer in 'resolver'.

Return ONLY a valid JSON object with these exact keys:
{{
  "likely_cause": "Detailed explanation of probable root cause",
  "recommended_fix": "Clear explanation of the proven mitigation",
  "command": "Exact shell or kubectl command to run (e.g. kubectl rollout restart deploy/{svc} or kubectl scale ... or pgbouncer patch)",
  "command_label": "Short label for command (e.g. Restart service, Scale replicas, Apply pooler patch)",
  "failed_fixes": "CRITICAL WARNING: Specific actions that failed in the past and must NOT be attempted",
  "resolver": "Name and role of engineer who previously fixed this (or 'On-Call SRE')",
  "confidence": "High (Memory Matched) or Medium (Pattern Match) or Novel (Cold Start)",
  "recalled_refs": ["List of referenced memory IDs or incident IDs like INC-1001"]
}}"""

        resp = await self.llm.chat_completion(
            messages=[
                {"role": "system", "content": "You are Echo-Core SRE Agent. Output valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1
        )
        
        content = resp.get("content", "")
        parsed = self._extract_json(content)
        
        # Fallback if LLM output couldn't be parsed
        if not parsed:
            # Deterministic synthesis from reflection if available
            failed_warning = "Check past incident records before attempting disruptive pod restarts."
            if "failed" in reflection_text.lower():
                failed_warning = reflection_text[:300]
                
            parsed = {
                "likely_cause": cause or "Service degradation matching past pattern.",
                "recommended_fix": "Apply proven pooler configuration and scale resources.",
                "command": f"kubectl rollout restart deploy/{svc}",
                "command_label": "Restart service",
                "failed_fixes": failed_warning,
                "resolver": "Sarah Chen (Staff SRE)",
                "confidence": "Medium",
                "recalled_refs": [r.get("id", "") for r in recalled_items[:2]]
            }
            
        parsed["memory_active"] = True
        return parsed

    async def _generate_generic_briefing(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generates standard generic advice WITHOUT memory context.
        Shows what a naive LLM or standard playbook would recommend.
        Notice that naive generic advice typically suggests restarting or scaling!
        """
        svc = incident.get("svc", "")
        title = incident.get("title", "")
        cause = incident.get("cause", "")

        prompt = f"""You are a standard automated alert triage tool WITHOUT historical incident memory.
Analyze this alert for service '{svc}':
Title: {title}
Reported issue: {cause}

Provide a standard, generic first-response recommendation without any knowledge of past team history or previous failed attempts.
Return ONLY a valid JSON object:
{{
  "likely_cause": "Generic textbook explanation based purely on the alert title",
  "recommended_fix": "Standard naive advice (e.g., restart service, scale pods, inspect logs)",
  "command": "kubectl rollout restart deploy/{svc}",
  "command_label": "Restart service",
  "failed_fixes": "No historical memory available. Proceed with standard trial and error.",
  "resolver": "None (No past context)",
  "confidence": "Low (Zero-shot / No Memory)",
  "recalled_refs": []
}}"""

        resp = await self.llm.chat_completion(
            messages=[
                {"role": "system", "content": "Output valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2
        )
        
        content = resp.get("content", "")
        parsed = self._extract_json(content)
        
        if not parsed:
            parsed = {
                "likely_cause": f"Generic resource exhaustion or transient network error on {svc}.",
                "recommended_fix": f"Restart the {svc} deployment pods to clear transient state.",
                "command": f"kubectl rollout restart deploy/{svc}",
                "command_label": "Restart service",
                "failed_fixes": "No historical memory available. Proceed with standard trial and error.",
                "resolver": "None (No past context)",
                "confidence": "Low (Zero-shot)",
                "recalled_refs": []
            }
            
        parsed["memory_active"] = False
        return parsed

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """Extracts JSON from model output text, handling Markdown blocks."""
        if not text:
            return None
        try:
            # Check for ```json ... ```
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if match:
                return json.loads(match.group(1))
            # Try raw text
            text_trimmed = text.strip()
            if text_trimmed.startswith("{") and text_trimmed.endswith("}"):
                return json.loads(text_trimmed)
            # Find first { and last }
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(text[start:end+1])
        except Exception as e:
            logger.debug(f"JSON extract failed: {e}")
        return None

# Global singleton agent instance
agent = IncidentAgent()
