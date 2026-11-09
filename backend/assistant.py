import logging
import json
import re
import time
from typing import Dict, Any, List, Optional
from backend.llm import llm, GroqLLM
from backend.memory import memory
from backend.agent import agent, RUNBOOKS
from backend.simulator import sim
from backend.database import db

logger = logging.getLogger("echo_core.assistant")

class SREAssistant:
    """
    Echo-Core Interactive SRE Chatbot.
    Provides context-aware on-call guidance when systems overload or incidents trigger.
    Proposes approval-gated mitigation actions that will NOT execute until the user clicks 'Allow'.
    """

    def __init__(self, llm_client: Optional[GroqLLM] = None):
        self.llm = llm_client or llm

    def _build_system_context(self, active_incidents: List[Dict[str, Any]], target_inc: Optional[Dict[str, Any]] = None) -> str:
        """Constructs real-time telemetry, incident, and runbook context for the prompt."""
        inc_summaries = []
        for inc in active_incidents:
            inc_summaries.append(
                f"- [{inc['id']}] Service: {inc['svc'].upper()} (Sev-{inc['sev']}): {inc['title']}. Root Cause: {inc.get('cause', 'N/A')}"
            )
        inc_str = "\n".join(inc_summaries) if inc_summaries else "All services currently nominal (0 active incidents)."

        target_context = ""
        if target_inc:
            target_svc = target_inc.get("svc", "")
            runbook = RUNBOOKS.get(target_svc, {}).get("procedures", [])
            target_context = f"""
SPECIFIC INCIDENT CONTEXT ({target_inc['id']}):
- Target Service: {target_svc}
- Severity: {target_inc.get('sev', 2)}
- Title: {target_inc.get('title')}
- Error Log: {target_inc.get('error_log')}
- Runbook Procedures: {json.dumps(runbook)}
- Memory-informed fix: {target_inc.get('briefing', {}).get('recommended_fix', 'Standard mitigation')}
- Anti-Pattern Warning: {target_inc.get('briefing', {}).get('failed_fixes', 'None recorded')}
"""

        context = f"""CURRENT CLUSTER TELEMETRY:
- Traffic RPS: {sim.rps:.0f} req/s
- p95 Latency: {sim.p95:.1f} ms (Threshold: 220ms, Critical: 300ms)
- Error Rate: {sim.err:.2f}% (Threshold: 1.0%, Critical: 2.0%)
- Cluster Status: {"CRITICAL OVERLOAD" if sim.p95 > 300 or sim.err > 2.0 else "ELEVATED STRAIN" if sim.p95 > 200 else "HEALTHY"}

ACTIVE INCIDENTS:
{inc_str}
{target_context}
"""
        return context

    def _generate_deterministic_response(
        self,
        user_message: str,
        active_incidents: List[Dict[str, Any]],
        target_inc: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Provides expert deterministic guidance when LLM API key is not present or offline."""
        msg_lower = user_message.lower()

        # Check if cascade or overload
        is_cascade = any(i.get("is_cascade") for i in active_incidents) or "cascade" in msg_lower or "overload" in msg_lower
        
        # Pick primary incident
        focus_inc = target_inc
        if not focus_inc and active_incidents:
            # Sort by severity (1 is critical)
            focus_inc = min(active_incidents, key=lambda x: x.get("sev", 3))

        svc = focus_inc.get("svc", "payments") if focus_inc else "payments"
        inc_id = focus_inc.get("id", "INC-2041") if focus_inc else "INC-2041"

        if "restart" in msg_lower and svc in ["payments", "postgres", "redis"]:
            content = (
                f"🚨 **STOP! CRITICAL ANTI-PATTERN PREVENTED**\n\n"
                f"Historical memory in `acme-sre` confirms that restarting the `{svc}` deployment during connection or memory exhaustion causes a violent connection stampede on Postgres (INC-1001).\n\n"
                f"**What to do instead:**\n"
                f"1. Do **NOT** execute `kubectl rollout restart deploy/{svc}`.\n"
                f"2. Apply PgBouncer pooler patch to absorb connection spikes without saturating the primary database.\n"
                f"3. I have prepared an approval-gated mitigation proposal below. Review and click **Allow** when ready."
            )
            suggestion = {
                "incident_id": inc_id,
                "svc": svc,
                "action_title": f"Route {svc.upper()} Traffic Through PgBouncer Connection Pool",
                "recommendation": f"Safely bounds database connection slots for {svc} to prevent primary lockup without restarting pods.",
                "command": f"kubectl patch deployment {svc} -p '{{\"spec\":{{\"template\":{{\"metadata\":{{\"annotations\":{{\"pool\":\"pgbouncer\"}}}}}}}}}}'",
                "risk_level": "LOW",
                "reasoning": "Memory bank acme-sre shows this resolved previous spike without repeating the disastrous pod restart storm."
            }
            return {"content": content, "suggestion": suggestion}

        if is_cascade or (focus_inc and focus_inc.get("sev") == 1):
            content = (
                f"⚠️ **SRE Emergency Triage: Cascading Outage Detected**\n\n"
                f"Current telemetry shows **p95 latency at {sim.p95:.1f}ms** and **error rate at {sim.err:.2f}%**.\n\n"
                f"**Root Cause Analysis:**\n"
                f"- The cascade root is `{svc}` due to database connection pool exhaustion.\n"
                f"- Upstream services (`orders`, `api-gateway`) are thread-locking on downstream RPC timeouts.\n\n"
                f"**Immediate Mitigation Plan:**\n"
                f"1. **Isolate:** Trip circuit breaker on `api-gateway` to reject degraded non-critical checkout traffic.\n"
                f"2. **Mitigate Root:** Expand PgBouncer pool limits on `{svc}` to clear socket queue.\n"
                f"3. **Do NOT restart:** Pod restart will trigger a connection storm!\n\n"
                f"I have staged the root-cause mitigation action below. Click **Allow** to authorize execution."
            )
            suggestion = {
                "incident_id": inc_id,
                "svc": svc,
                "action_title": f"Tune Max Connections & Scale PgBouncer on {svc.upper()}",
                "recommendation": f"Expands connection pool limit from 100 to 250 connections and stabilizes RPC throughput on {svc}.",
                "command": f"kubectl scale deployment {svc} --replicas=6 && kubectl annotate deploy {svc} pool.tier=high-capacity",
                "risk_level": "LOW",
                "reasoning": f"Proven mitigation from incident records for {svc} connection pool starvation."
            }
            return {"content": content, "suggestion": suggestion}

        # Default helpful SRE guidance
        content = (
            f"🤖 **Echo-Core SRE Assistant Operational**\n\n"
            f"Monitoring {len(active_incidents)} active incident(s) across the dependency graph.\n"
            f"- Cluster p95: **{sim.p95:.1f}ms** | Error rate: **{sim.err:.2f}%**\n"
        )
        if focus_inc:
            content += (
                f"\n**Active Issue on `{svc}` ({focus_inc['id']}):** {focus_inc.get('title')}\n"
                f"**Recommended mitigation:** {focus_inc.get('briefing', {}).get('recommended_fix', 'Inspect connection pools and apply rate limits.')}\n\n"
                f"You can review the staged mitigation card below or ask me about any service."
            )
            suggestion = {
                "incident_id": inc_id,
                "svc": svc,
                "action_title": f"Apply Nominal Scaling & Connection Check on {svc.upper()}",
                "recommendation": f"Relieves queue pressure on {svc} by scaling replicas to absorb traffic.",
                "command": f"kubectl scale deploy/{svc} --replicas=4",
                "risk_level": "LOW",
                "reasoning": f"Standard safe remediation runbook for {svc} latency spikes."
            }
        else:
            content += "\nAll services operating within SLA. You can simulate an incident or cascade using the buttons above to test response workflows."
            suggestion = None

        return {"content": content, "suggestion": suggestion}

    def _extract_suggestion_block(self, text: str) -> tuple[str, Optional[Dict[str, Any]]]:
        """Extracts <suggestion> JSON block if present, returning cleaned text and suggestion dict."""
        pattern = r"<suggestion>\s*({.*?})\s*</suggestion>"
        match = re.search(pattern, text, re.DOTALL)
        if not match:
            # Check for markdown json block matching suggestion format
            json_pattern = r"```(?:json)?\s*(\{\s*\"action_title\".*?\})\s*```"
            match = re.search(json_pattern, text, re.DOTALL)

        if match:
            try:
                sug_data = json.loads(match.group(1))
                clean_text = text[:match.start()].strip() + "\n" + text[match.end():].strip()
                return clean_text.strip(), sug_data
            except Exception as e:
                logger.warning(f"Failed to parse suggestion JSON: {e}")
        return text, None

    async def answer_query(
        self,
        user_message: str,
        incident_id: Optional[str] = None,
        session_id: str = "default"
    ) -> Dict[str, Any]:
        """
        Processes an SRE query, synthesizes advice, and proposes approval-gated actions.
        """
        # Save user message to SQLite
        await db.save_chat_message(
            role="user",
            content=user_message,
            incident_id=incident_id,
            session_id=session_id
        )
        await db.log_audit(
            action="CHAT_QUERY",
            incident_id=incident_id,
            actor="On-Call SRE",
            details=f"User asked: {user_message[:100]}"
        )

        # Retrieve active incidents from simulator
        active_incidents = [i for i in sim.incidents.values() if i.get("status") == "active"]
        target_inc = sim.incidents.get(incident_id) if incident_id else None

        # Build context
        system_context = self._build_system_context(active_incidents, target_inc)

        # Check if LLM client is available
        if not self.llm.client:
            det = self._generate_deterministic_response(user_message, active_incidents, target_inc)
            raw_content = det["content"]
            sug_raw = det.get("suggestion")
        else:
            # Construct Groq LLM prompt
            prompt = f"""You are Echo-Core AI SRE Assistant — an elite Site Reliability Engineer with persistent memory.
Your mission is to guide on-call engineers when systems overload or alerts trigger.

{system_context}

USER MESSAGE:
"{user_message}"

INSTRUCTIONS:
1. Provide concise, expert, bullet-pointed SRE guidance.
2. If the user mentions restarting a database, cache, or payments service, WARN AGAINST ANTI-PATTERNS (e.g. connection stampedes, cache flush storms).
3. If actionable remediation is needed, propose a concrete shell/kubectl mitigation command inside a `<suggestion>` block formatted as strictly valid JSON:
<suggestion>
{{
  "action_title": "Clear concise title of mitigation action",
  "svc": "target-service-name",
  "recommendation": "What this mitigation does and expected impact",
  "command": "kubectl or ops command",
  "risk_level": "LOW, MODERATE, HIGH, or CRITICAL",
  "reasoning": "Why this is recommended and what historical incident or runbook supports it"
}}
</suggestion>
4. CRITICAL: Inform the operator that the command is safely staged in the Approval Gate and will NOT execute until they click 'Allow'.
"""
            try:
                response = await self.llm.chat_completion(
                    messages=[
                        {"role": "system", "content": "You are Echo-Core AI SRE Assistant. Respond with structured markdown."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.2,
                    max_tokens=900
                )
                raw_content = response.get("content", "")
            except Exception as e:
                logger.error(f"LLM query error: {e}")
                det = self._generate_deterministic_response(user_message, active_incidents, target_inc)
                raw_content = det["content"]
                sug_raw = det.get("suggestion")

            clean_text, sug_raw = self._extract_suggestion_block(raw_content)
            raw_content = clean_text

        # If a suggestion was produced, persist it into the database with 'pending' status
        saved_suggestion = None
        if sug_raw:
            try:
                target_inc_id = sug_raw.get("incident_id") or (target_inc.get("id") if target_inc else None)
                if not target_inc_id:
                    target_inc_id = active_incidents[0]["id"] if active_incidents else "INC-GENERAL"

                saved_suggestion = await db.create_suggestion(
                    incident_id=target_inc_id,
                    svc=sug_raw.get("svc", "payments"),
                    action_title=sug_raw.get("action_title", "Mitigation Action"),
                    recommendation=sug_raw.get("recommendation", "Apply operational mitigation."),
                    command=sug_raw.get("command", "kubectl get pods"),
                    risk_level=sug_raw.get("risk_level", "LOW"),
                    reasoning=sug_raw.get("reasoning", "Recommended by Echo-Core SRE Assistant.")
                )
            except Exception as e:
                logger.error(f"Failed to persist suggestion: {e}")

        # Save assistant message to SQLite
        sug_id = saved_suggestion["id"] if saved_suggestion else None
        await db.save_chat_message(
            role="assistant",
            content=raw_content,
            incident_id=incident_id,
            suggestion_id=sug_id,
            session_id=session_id
        )

        return {
            "role": "assistant",
            "content": raw_content,
            "suggestion": saved_suggestion,
            "session_id": session_id,
            "timestamp": int(time.time() * 1000)
        }

# Global singleton assistant instance
assistant = SREAssistant()
