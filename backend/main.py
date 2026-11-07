import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
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

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Autonomous on-call incident response agent with Hindsight persistent memory"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Background simulation runner
async def simulation_ticker():
    """Periodically ticks metrics and generates realistic logs."""
    while True:
        sim.tick_metrics()
        sim.generate_log()
        await asyncio.sleep(1.2)

@app.on_event("startup")
async def startup_event():
    logger.info("Starting Echo-Core backend...")
    # Verify Hindsight bank asynchronously
    asyncio.create_task(memory.ensure_bank())
    # Start background telemetry generator
    asyncio.create_task(simulation_ticker())

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
    resolver: Optional[str] = "On-Call SRE"

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
    """Returns all incidents."""
    return list(sim.incidents.values())

@app.get("/api/incidents/{inc_id}")
async def get_incident(inc_id: str):
    """Fetches single incident with full memory & generic briefings."""
    inc = sim.incidents.get(inc_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    # If briefings not already computed or cached, compute them
    if "briefing" not in inc:
        analysis = await agent.analyze_incident(inc)
        inc["briefing"] = analysis["memory_informed"]
        inc["generic_briefing"] = analysis["generic"]
        inc["recalled_items"] = analysis["recalled_items"]

    return inc

@app.post("/api/incidents/simulate")
async def simulate_incident(req: SimulateRequest):
    """
    Simulates a new incident or triggers a specific demo scenario.
    Demo 1 = Cold Start (novel issue, generic advice)
    Demo 5 = Similar Recall (matches past incident)
    Demo 20 = Failed Fix Warning (warns against disastrous past failed fix)
    """
    res = sim.trigger_incident(
        svc=req.service,
        sev=req.severity,
        title=req.title,
        cause=req.cause,
        scenario_num=req.scenario_num
    )
    inc = res["incident"]
    
    # Automatically compute dual-mode agent analysis
    analysis = await agent.analyze_incident(inc)
    inc["briefing"] = analysis["memory_informed"]
    inc["generic_briefing"] = analysis["generic"]
    inc["recalled_items"] = analysis["recalled_items"]

    return {
        "incident": inc,
        "merged": res["merged"]
    }

@app.post("/api/incidents/{inc_id}/command")
async def run_command(inc_id: str, req: CommandRequest):
    """
    Executes an approved mitigation command. Requires service name confirmation.
    """
    inc = sim.incidents.get(inc_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    # Verify confirmation for mutating commands
    if req.service_confirm.strip().lower() != inc["svc"].strip().lower():
        raise HTTPException(status_code=400, detail="Service confirmation mismatch")

    result = sim.execute_command(inc_id, req.command_key, req.command_str)
    
    # Log command execution
    sim.logs.insert(0, {
        "t": int(asyncio.get_event_loop().time() * 1000),
        "l": "INFO" if result.get("success") else "ERROR",
        "s": inc["svc"],
        "m": f"$ {req.command_str or req.command_key} -> {result.get('message')}"
    })
    
    return result

@app.post("/api/incidents/{inc_id}/resolve")
async def resolve_incident(inc_id: str, req: ResolveRequest, background_tasks: BackgroundTasks):
    """
    Marks incident resolved, takes engineer feedback, and retains the full record into Hindsight.
    """
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
        "resolver": req.resolver
    }

    # Retain asynchronously into Hindsight bank so memory gets smarter
    background_tasks.add_task(memory.retain_incident, inc, feedback)

    return {
        "success": True,
        "incident_id": inc_id,
        "retained": True,
        "message": f"Incident {inc_id} resolved and retained into Hindsight memory."
    }

@app.get("/api/memory/stats")
async def memory_stats():
    """Returns memory bank stats and health."""
    return await memory.get_stats()

@app.post("/api/memory/seed")
async def trigger_seed(background_tasks: BackgroundTasks, limit: Optional[int] = 10):
    """Triggers memory ingestion in background."""
    background_tasks.add_task(seed_memory, limit=limit)
    return {"message": f"Seeding background task started (limit: {limit})"}

# Server-Sent Events (SSE) Stream
@app.get("/api/events")
async def sse_events(request: Request):
    """
    Streams live metrics, active incidents, logs, and Hindsight health.
    """
    async def event_generator():
        last_log_t = 0
        while True:
            if await request.is_disconnected():
                break

            # Collect current snapshot
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

            yield f"data: {json.dumps(payload)}\n\n"
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

# Serve Frontend
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = settings.FRONTEND_DIR / "sentinelops.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Echo-Core Frontend not found</h1>", status_code=404)
    with open(index_file, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

# Mount frontend directory for static assets
app.mount("/static", StaticFiles(directory=str(settings.FRONTEND_DIR)), name="static")
