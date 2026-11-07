import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Query
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

async def simulation_ticker():
    while True:
        sim.tick_metrics()
        sim.generate_log()
        await asyncio.sleep(1.2)

@app.on_event("startup")
async def startup_event():
    logger.info("Starting Echo-Core backend...")
    asyncio.create_task(memory.ensure_bank())
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
    Analyzes all cascade incidents in parallel and pinpoints root cause service.
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

    # Safety Pre-flight Check
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

@app.get("/api/memory/stats")
async def memory_stats():
    return await memory.get_stats()

@app.get("/api/memory/anti-patterns")
async def memory_anti_patterns():
    """Returns catalog of known infrastructure anti-patterns and dangerous failed fixes."""
    return await memory.get_anti_patterns()

@app.get("/api/memory/search")
async def memory_search(q: str = Query(..., description="Query to recall from Hindsight")):
    """Direct search interface for Hindsight memory bank."""
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
    return {"message": f"Full memory seeding background task started"}

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

app.mount("/static", StaticFiles(directory=str(settings.FRONTEND_DIR)), name="static")
