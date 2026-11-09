import asyncio
import json
import random
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Query, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.config import settings
from backend.database import db
from backend.memory import memory
from backend.agent import agent
from backend.simulator import sim, DEMO_SCENARIOS
from backend.assistant import assistant
from scripts.seed_memory import seed_memory

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("echo_core.api")

# Background simulation runner
ticker_task: Optional[asyncio.Task] = None

async def simulation_ticker():
    try:
        while True:
            sim.tick_metrics()
            sim.generate_log()
            await asyncio.sleep(1.2)
    except asyncio.CancelledError:
        pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    global ticker_task
    logger.info("Starting Echo-Core backend with SQLite persistence...")
    db.init_db()
    asyncio.create_task(memory.ensure_bank())
    ticker_task = asyncio.create_task(simulation_ticker())
    yield
    logger.info("Shutting down Echo-Core backend...")
    if ticker_task:
        ticker_task.cancel()
    await memory.close()

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Autonomous on-call incident response agent with Hindsight persistent memory and SQLite database",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request Models
class SimulateRequest(BaseModel):
    service: Optional[str] = None
    severity: Optional[int] = None
    title: Optional[str] = None
    cause: Optional[str] = None
    scenario_num: Optional[int] = None

class CommandRequest(BaseModel):
    command_key: str
    command_str: Optional[str] = None
    service_confirm: str

class ResolveRequest(BaseModel):
    notes: Optional[str] = "Resolved by on-call engineer."
    worked: bool = True
    resolver: Optional[str] = "Sarah Chen (Staff SRE)"
    rating: Optional[int] = 5

class ChatRequest(BaseModel):
    message: str
    incident_id: Optional[str] = None
    session_id: Optional[str] = "default"

class SuggestionApproveRequest(BaseModel):
    actor: Optional[str] = "Sarah Chen (Staff SRE)"

class SuggestionRejectRequest(BaseModel):
    reason: Optional[str] = "Manual operator override"
    actor: Optional[str] = "Sarah Chen (Staff SRE)"

# REST Endpoints
@app.get("/api/health")
async def health_check():
    stats = await memory.get_stats()
    db_stats = await db.get_db_stats()
    return {
        "status": "healthy",
        "project": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "hindsight": stats,
        "database": db_stats,
        "active_incidents": len([i for i in sim.incidents.values() if i.get("status") == "active"])
    }

@app.get("/api/incidents")
async def list_incidents():
    return list(sim.incidents.values())

@app.get("/api/incidents/history")
async def get_incident_history(limit: int = 50):
    """Returns resolved incidents stored permanently in SQLite database."""
    return await db.get_history_incidents(limit=limit)

@app.get("/api/incidents/{inc_id}")
async def get_incident(inc_id: str):
    inc = sim.incidents.get(inc_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    if "briefing" not in inc:
        analysis = await agent.analyze_incident(inc)
        inc["briefing"] = analysis["memory_informed"]
        inc["generic_briefing"] = analysis["generic"]
        inc["comparison"] = analysis.get("comparison", {})
        inc["recalled_items"] = analysis["recalled_items"]
        await db.save_incident(inc)

    return inc

@app.post("/api/incidents/simulate")
async def simulate_incident(req: SimulateRequest):
    res = sim.trigger_incident(
        svc=req.service,
        sev=req.severity,
        title=req.title,
        cause=req.cause,
        scenario_num=req.scenario_num
    )
    inc = res["incident"]
    
    analysis = await agent.analyze_incident(inc)
    inc["briefing"] = analysis["memory_informed"]
    inc["generic_briefing"] = analysis["generic"]
    inc["comparison"] = analysis.get("comparison", {})
    inc["recalled_items"] = analysis["recalled_items"]
    await db.save_incident(inc)

    return {
        "incident": inc,
        "merged": res["merged"]
    }

@app.post("/api/incidents/cascade")
async def trigger_cascade():
    """
    Triggers multi-service cascading outage across the dependency topology.
    Analyzes all cascade incidents concurrently in parallel and persists to SQLite.
    """
    cascade_incidents = sim.trigger_cascade()

    async def _analyze_one(inc):
        analysis = await agent.analyze_incident(inc)
        inc["briefing"] = analysis["memory_informed"]
        inc["generic_briefing"] = analysis["generic"]
        inc["comparison"] = analysis.get("comparison", {})
        inc["recalled_items"] = analysis["recalled_items"]
        await db.save_incident(inc)
        return inc

    analyzed = await asyncio.gather(*[_analyze_one(inc) for inc in cascade_incidents])

    return {
        "cascade_incidents": list(analyzed),
        "root_cause_service": "payments",
        "cascade_chain": ["payments", "postgres", "orders", "api-gateway"]
    }

@app.post("/api/incidents/{inc_id}/command")
async def run_command(inc_id: str, req: CommandRequest):
    inc = sim.incidents.get(inc_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    if req.service_confirm.strip().lower() != inc["svc"].strip().lower():
        raise HTTPException(status_code=400, detail="Service confirmation mismatch")

    safety_check = await agent.check_command_safety(inc["svc"], req.command_str or req.command_key)
    result = sim.execute_command(inc_id, req.command_key, req.command_str)
    
    sim.logs.insert(0, {
        "t": int(asyncio.get_event_loop().time() * 1000),
        "l": "ERROR" if result.get("catastrophic") else "INFO",
        "s": inc["svc"],
        "m": f"$ {req.command_str or req.command_key} -> {result.get('message')}"
    })
    
    result["safety_check"] = safety_check
    await db.save_incident(inc)
    return result

@app.post("/api/incidents/{inc_id}/resolve")
async def resolve_incident(inc_id: str, req: ResolveRequest, background_tasks: BackgroundTasks):
    inc = sim.incidents.get(inc_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    inc["status"] = "resolved"
    inc["resolver"] = req.resolver
    inc["fix_worked"] = inc.get("briefing", {}).get("recommended_fix", "Mitigation applied")
    inc["fixes_failed"] = inc.get("briefing", {}).get("failed_fixes", "None recorded")

    feedback = {
        "worked": req.worked,
        "notes": req.notes,
        "rating": req.rating,
        "resolver": req.resolver
    }

    # Easing cluster telemetry back to nominal baseline upon engineer resolution
    sim.p95 = round(random.uniform(142.0, 185.0), 1)
    sim.err = round(random.uniform(0.12, 0.40), 2)

    await db.save_incident(inc)
    await db.log_audit(
        action="INCIDENT_RESOLVE",
        incident_id=inc_id,
        svc=inc["svc"],
        actor=req.resolver or "On-Call SRE",
        details=f"Resolution note: {req.notes} (Worked: {req.worked}, Rating: {req.rating}/5)"
    )

    background_tasks.add_task(memory.retain_incident, inc, feedback)

    return {
        "success": True,
        "incident_id": inc_id,
        "retained": True,
        "message": f"Incident {inc_id} resolved, stored in SQLite database, and retained into Hindsight memory."
    }

@app.get("/api/incidents/{inc_id}/verify")
async def verify_service_health(inc_id: str):
    """Verifies whether service telemetry returned to normal operating baseline."""
    inc = sim.incidents.get(inc_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    is_resolved = inc.get("status") == "resolved"
    healthy = is_resolved or (sim.p95 < 250.0 and sim.err < 1.5)

    display_p95 = round(sim.p95 if not is_resolved else random.uniform(138.0, 185.0), 1)
    display_err = round(sim.err if not is_resolved else random.uniform(0.12, 0.38), 2)

    return {
        "incident_id": inc_id,
        "service": inc["svc"],
        "current_p95": display_p95,
        "current_err": display_err,
        "is_healthy": healthy,
        "status": "RECOVERED" if healthy else "DEGRADED"
    }

@app.get("/api/incidents/{inc_id}/postmortem")
async def generate_postmortem(inc_id: str):
    """
    Generates a comprehensive SRE Incident Post-Mortem in Markdown.
    """
    inc = sim.incidents.get(inc_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    brief = inc.get("briefing", {})
    comp = inc.get("comparison", {})
    t_start = datetime.fromtimestamp(inc["t"] / 1000).strftime("%Y-%m-%d %H:%M:%S UTC")

    markdown = f"""# Incident Post-Mortem: {inc['id']} ({inc['svc'].upper()})
**Status:** {inc.get('status', 'active').upper()}  
**Severity:** SEV-{inc.get('sev', 2)}  
**Reported At:** {t_start}  
**Lead Resolver:** {inc.get('resolver', brief.get('resolver', 'Sarah Chen (Staff SRE)'))}  
**Memory Bank:** `{settings.HINDSIGHT_BANK_ID}`
**Persistence:** Stored in SQLite Database (`echo_core.db`)

---

## 1. Executive Summary
On {t_start}, an alert was triggered on `{inc['svc']}` with title: **"{inc.get('title')}"**.
The issue was analyzed by Echo-Core using persistent Hindsight team memory and SQLite audit logging.
The root cause was identified as:
> *{brief.get('likely_cause', inc.get('cause'))}*

---

## 2. Root Cause & Historical Context
- **Historical Recurrence:** Matched past incidents in team bank `{settings.HINDSIGHT_BANK_ID}`.
- **Confidence Level:** {brief.get('confidence', 'High (Memory Matched)')}
- **Retrieved Memory References:** {', '.join(brief.get('recalled_refs', ['INC-1001']))}

### 🚨 Critical Anti-Pattern Prevented:
{brief.get('failed_fixes', 'No previous failed attempts recorded.')}

---

## 3. Remediation & Action Taken
- **Proven Fix Executed:** {brief.get('recommended_fix', 'Standard mitigation applied.')}
- **Command:**
```bash
{brief.get('command', 'kubectl rollout status deploy/' + inc['svc'])}
```
- **Risk Avoidance Delta:** {comp.get('risk_prevented', 'Memory-backed triage avoided naive trial-and-error.')}

---

## 4. Timeline
"""
    for ts, ev in inc.get("ev", []):
        t_str = datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S")
        markdown += f"- **{t_str}** - {ev}\n"

    markdown += f"""
---

## 5. Lessons Learned & Knowledge Retention
1. **Automated Memory Ingestion:** This resolution and engineer feedback have been retained into Hindsight bank `{settings.HINDSIGHT_BANK_ID}`.
2. **Persistent Audit Trail:** Incident lifecycle and execution checkpoints are recorded in SQLite `audit_log` table.
3. Future on-call engineers facing similar symptoms on `{inc['svc']}` will immediately recall this post-mortem and mitigation path.

*Generated autonomously by Echo-Core Incident Response Agent.*
"""
    return PlainTextResponse(markdown, media_type="text/markdown")

# ====================
# CHATBOT ENDPOINTS
# ====================
@app.post("/api/chat")
async def chat_with_assistant(req: ChatRequest):
    """
    Interactive SRE Assistant chat endpoint.
    Synthesizes overload triage guidance and proposes approval-gated mitigation actions.
    """
    res = await assistant.answer_query(
        user_message=req.message,
        incident_id=req.incident_id,
        session_id=req.session_id or "default"
    )
    return res

@app.get("/api/chat/history")
async def get_chat_history(session_id: str = "default", limit: int = 50):
    """Retrieves chat message history from SQLite database."""
    messages = await db.get_chat_history(session_id=session_id, limit=limit)
    return {"session_id": session_id, "messages": messages}

@app.post("/api/chat/clear")
async def clear_chat(session_id: str = "default"):
    """Clears chat messages for session."""
    await db.clear_chat_history(session_id=session_id)
    return {"success": True, "message": "Chat history cleared."}

# ====================
# APPROVAL GATE ENDPOINTS
# ====================
@app.get("/api/suggestions")
async def list_suggestions(status: Optional[str] = None, limit: int = 50):
    """Lists approval-gated AI mitigation suggestions."""
    return await db.list_suggestions(status=status, limit=limit)

@app.post("/api/suggestions/{sug_id}/approve")
async def approve_suggestion(sug_id: int, req: SuggestionApproveRequest):
    """
    User clicks 'Allow': executes the AI proposed mitigation action,
    eases system telemetry, marks suggestion as approved, and logs to audit trail.
    """
    sug = await db.get_suggestion(sug_id)
    if not sug:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    if sug["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Suggestion already {sug['status']}")

    svc = sug["svc"]
    inc_id = sug["incident_id"]
    command_str = sug["command"]

    # Pre-flight safety verification
    safety_check = await agent.check_command_safety(svc, command_str)
    if safety_check.get("is_dangerous") and "PROHIBITED" in safety_check.get("warning", ""):
        raise HTTPException(status_code=400, detail=f"Safety check blocked: {safety_check.get('warning')}")

    # Execute mitigation
    exec_result = sim.execute_command(inc_id, "mitigate", command_str)
    exec_message = exec_result.get("message", "Mitigation executed successfully.")

    # Ease cluster telemetry metrics
    sim.p95 = max(138.0, round(sim.p95 * 0.60, 1))
    sim.err = max(0.12, round(sim.err * 0.35, 2))

    # Update suggestion in SQLite
    updated = await db.update_suggestion_status(
        sug_id=sug_id,
        status="approved",
        actor=req.actor or "On-Call SRE",
        execution_result=exec_message
    )

    # Log to audit trail
    await db.log_audit(
        action="SUGGESTION_APPROVE",
        incident_id=inc_id,
        svc=svc,
        actor=req.actor or "On-Call SRE",
        details=f"Approved AI Action: '{sug['action_title']}' -> {exec_message}"
    )

    # Add stream log
    sim.logs.insert(0, {
        "t": int(asyncio.get_event_loop().time() * 1000),
        "l": "INFO",
        "s": svc,
        "m": f"✅ [APPROVAL-GATE] Action '{sug['action_title']}' authorized by {req.actor}. Command executed: {command_str}"
    })

    # Append system message to chat
    await db.save_chat_message(
        role="assistant",
        content=f"✅ **Action Authorized & Executed:** `{sug['action_title']}` on `{svc}`.\n- **Result:** {exec_message}\n- Cluster telemetry stabilized (p95: {sim.p95}ms, Error: {sim.err}%).",
        incident_id=inc_id
    )

    return {
        "success": True,
        "suggestion": updated,
        "execution_result": exec_result,
        "metrics": {"p95": sim.p95, "err": sim.err}
    }

@app.post("/api/suggestions/{sug_id}/reject")
async def reject_suggestion(sug_id: int, req: SuggestionRejectRequest):
    """
    User clicks 'Reject': discards the AI proposed suggestion without executing.
    """
    sug = await db.get_suggestion(sug_id)
    if not sug:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    if sug["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Suggestion already {sug['status']}")

    updated = await db.update_suggestion_status(
        sug_id=sug_id,
        status="rejected",
        actor=req.actor or "On-Call SRE",
        rejected_reason=req.reason or "Rejected by operator"
    )

    await db.log_audit(
        action="SUGGESTION_REJECT",
        incident_id=sug["incident_id"],
        svc=sug["svc"],
        actor=req.actor or "On-Call SRE",
        details=f"Rejected AI Action '{sug['action_title']}': {req.reason}"
    )

    # Append notification in chat
    await db.save_chat_message(
        role="assistant",
        content=f"❌ **Action Rejected by Operator:** `{sug['action_title']}` on `{sug['svc']}`. (Reason: {req.reason})",
        incident_id=sug["incident_id"]
    )

    return {
        "success": True,
        "suggestion": updated
    }

@app.post("/api/incidents/{inc_id}/suggest")
async def generate_incident_suggestion(inc_id: str):
    """Explicitly triggers AI to formulate an approval-gated proposal for this incident."""
    inc = sim.incidents.get(inc_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    briefing = inc.get("briefing", {})
    cmd = briefing.get("command", f"kubectl scale deploy/{inc['svc']} --replicas=5")
    title = briefing.get("command_label", f"Mitigate {inc['svc'].upper()} Pressure")
    rec = briefing.get("recommended_fix", "Apply connection pool configuration and safe horizontal pod scaling.")

    sug = await db.create_suggestion(
        incident_id=inc_id,
        svc=inc["svc"],
        action_title=f"AI Proposed: {title}",
        recommendation=rec,
        command=cmd,
        risk_level="LOW" if "scale" in cmd or "patch" in cmd else "MODERATE",
        reasoning=f"Generated from memory briefing of {inc_id} ({briefing.get('confidence', 'Memory-backed')})."
    )

    await db.log_audit(
        action="SUGGESTION_CREATE",
        incident_id=inc_id,
        svc=inc["svc"],
        actor="Echo-Core Agent",
        details=f"Proposed mitigation: {sug['action_title']}"
    )

    return sug

# ====================
# DATABASE & AUDIT ENDPOINTS
# ====================
@app.get("/api/database/stats")
async def get_database_stats():
    """Returns database size, incident count, alert count, and suggestion count."""
    return await db.get_db_stats()

@app.get("/api/database/alerts")
async def get_database_alerts(limit: int = 50):
    """Returns persisted alert feed."""
    return await db.get_alerts(limit=limit)

@app.get("/api/audit")
async def get_audit_trail(limit: int = 100):
    """Returns operator and agent audit trail."""
    return await db.get_audit_logs(limit=limit)

# ====================
# MEMORY ENDPOINTS
# ====================
@app.get("/api/memory/stats")
async def memory_stats():
    return await memory.get_stats()

@app.get("/api/memory/anti-patterns")
async def memory_anti_patterns():
    return await memory.get_anti_patterns()

@app.get("/api/memory/search")
async def memory_search(q: str = Query(..., description="Query to recall from Hindsight")):
    results = await memory.search_memories(q)
    return {
        "query": q,
        "bank_id": settings.HINDSIGHT_BANK_ID,
        "count": len(results),
        "results": results
    }

@app.post("/api/memory/seed")
async def trigger_seed(background_tasks: BackgroundTasks, limit: Optional[int] = None):
    background_tasks.add_task(seed_memory, limit=limit)
    return {"message": "Memory seeding background task started"}

# Server-Sent Events (SSE) Stream
@app.get("/api/events")
async def sse_events(request: Request):
    async def event_generator():
        last_log_t = 0
        while True:
            if await request.is_disconnected():
                break

            active_list = [i for i in sim.incidents.values() if i.get("status") == "active"]
            new_logs = [l for l in sim.logs if l["t"] > last_log_t][:10]
            if sim.logs:
                last_log_t = sim.logs[0]["t"]

            # Quick DB counts for UI pills
            db_stats = {
                "active_incidents": len(active_list),
                "is_healthy": True
            }

            payload = {
                "metrics": {
                    "rps": sim.rps,
                    "p95": sim.p95,
                    "err": sim.err,
                    "inc": len(active_list),
                    "history": sim.history
                },
                "incidents": active_list,
                "logs": new_logs,
                "database": db_stats,
                "hindsight": {
                    "is_healthy": memory.is_healthy,
                    "last_error": memory.last_error
                }
            }

            try:
                data_str = json.dumps(jsonable_encoder(payload), default=str)
                yield f"data: {data_str}\n\n"
            except Exception as sse_err:
                logger.error(f"Error encoding SSE payload: {sse_err}")
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

# Serve Frontend
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = settings.FRONTEND_DIR / "sentinelops.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Echo-Core Frontend not found</h1>", status_code=404)
    with open(index_file, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

app.mount("/static", StaticFiles(directory=str(settings.FRONTEND_DIR)), name="static")
