import asyncio
import json
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
from backend.memory import memory
from backend.agent import agent
from backend.simulator import sim, DEMO_SCENARIOS
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
    logger.info("Starting Echo-Core backend...")
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
    description="Autonomous on-call incident response agent with Hindsight persistent memory",
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

# REST Endpoints
@app.get("/api/health")
async def health_check():
    stats = await memory.get_stats()
    return {
        "status": "healthy",
        "project": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "hindsight": stats,
        "active_incidents": len([i for i in sim.incidents.values() if i.get("status") == "active"])
    }

@app.get("/api/incidents")
async def list_incidents():
    return list(sim.incidents.values())

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

    return {
        "incident": inc,
        "merged": res["merged"]
    }

@app.post("/api/incidents/cascade")
async def trigger_cascade():
    """
    Triggers multi-service cascading outage across the dependency topology.
    Analyzes all cascade incidents concurrently in parallel.
    """
    cascade_incidents = sim.trigger_cascade()

    async def _analyze_one(inc):
        analysis = await agent.analyze_incident(inc)
        inc["briefing"] = analysis["memory_informed"]
        inc["generic_briefing"] = analysis["generic"]
        inc["comparison"] = analysis.get("comparison", {})
        inc["recalled_items"] = analysis["recalled_items"]
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

    background_tasks.add_task(memory.retain_incident, inc, feedback)

    return {
        "success": True,
        "incident_id": inc_id,
        "retained": True,
        "message": f"Incident {inc_id} resolved and retained into Hindsight memory."
    }

@app.get("/api/incidents/{inc_id}/verify")
async def verify_service_health(inc_id: str):
    """Verifies whether service telemetry returned to normal operating baseline."""
    inc = sim.incidents.get(inc_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    healthy = sim.p95 < 220 and sim.err < 1.0
    return {
        "incident_id": inc_id,
        "service": inc["svc"],
        "current_p95": sim.p95,
        "current_err": sim.err,
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

---

## 1. Executive Summary
On {t_start}, an alert was triggered on `{inc['svc']}` with title: **"{inc.get('title')}"**.
The issue was analyzed by Echo-Core using persistent Hindsight team memory.
The root cause was identified as:
> *{brief.get('likely_cause', inc.get('cause'))}*

---

## 2. Root Cause & Historical Context
- **Historical Recurrence:** Matched past incidents in team bank `acme-sre`.
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
2. Future on-call engineers facing similar symptoms on `{inc['svc']}` will immediately recall this post-mortem and mitigation path.

*Generated autonomously by Echo-Core Incident Response Agent.*
"""
    return PlainTextResponse(markdown, media_type="text/markdown")

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
