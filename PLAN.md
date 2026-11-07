# PLAN.md: Echo-Core Incident Response Agent with Hindsight Persistent Memory

## 1. Project Overview & Goal
**Echo-Core** is an on-call Incident Response Agent built for **HackwithBengaluru 2.0**.
It integrates **Hindsight memory** (`retain`, `recall`, `reflect`) and **Groq LLMs** (`openai/gpt-oss-120b` with `qwen/qwen3.8-27b` fallback) with the existing incident command center UI.
The core premise: **Echo-Core gets measurably smarter with every incident**. It remembers every past symptom, root cause, working fix, and crucially, **fixes that failed**, preventing on-call engineers from repeating disastrous mistakes.

---

## 2. Architecture & File Structure

```
MIC2/
├── .env.example                 # Template for required environment variables
├── .env                         # Local environment configuration (GITIGNORED)
├── .gitignore                   # Ignores .env, __pycache__, logs
├── README.md                    # Architecture diagram, Hindsight usage, run guide
├── requirements.txt             # Python dependencies
├── backend/
│   ├── __init__.py
│   ├── config.py                # Pydantic Settings & environment variables
│   ├── memory.py                # Hindsight wrapper: retain_incident, recall_for_alert, reflect_briefing
│   ├── llm.py                   # Groq client with retry, fallback models, structured tool calling
│   ├── agent.py                 # Incident response agent with tool loops & dual-mode briefing
│   ├── simulator.py             # Realistic telemetry (RPS, p95, error rates) & alert/log generator
│   └── main.py                  # FastAPI REST endpoints + Server-Sent Events (SSE)
├── data/
│   └── seed_incidents.json      # 45 realistic historical incidents with patterns & failed fixes
├── scripts/
│   └── seed_memory.py           # Ingestion script to retain seed incidents into Hindsight bank
└── frontend/
    └── sentinelops.html         # Echo-Core dashboard connected via REST/SSE, with Memory View & Toggle
```

---

## 3. Interfaces & Data Contracts

### 3.1. Hindsight Memory Wrapper (`backend/memory.py`)
```python
class IncidentMemory:
    def __init__(self, base_url: str, api_key: str, bank_id: str = "acme-sre"):
        ...
    
    async def ensure_bank(self) -> bool:
        """Verifies or creates the team memory bank in Hindsight."""
        
    async def recall_for_alert(self, service: str, error_signature: str, top_k: int = 5) -> List[Dict]:
        """Hybrid recall (BM25 + Semantic + Temporal) from Hindsight bank."""
        
    async def reflect_briefing(self, service: str, symptoms: str, error_log: str) -> Dict[str, Any]:
        """
        Invokes Hindsight reflect() to synthesize:
        - likely_cause: str
        - what_worked: str
        - what_failed: List[str]  <-- Critical star feature
        - who_solved_it: str
        - past_incident_refs: List[str]
        """

    async def retain_incident(self, incident: Dict, feedback: Dict) -> bool:
        """
        Retains resolved incident record, resolution actions, and human feedback:
        (e.g., 'Agent suggestion worked', 'Restart failed, scaling succeeded').
        """
        
    def check_health(self) -> Dict[str, Any]:
        """Returns Hindsight connection status and reachability."""
```

### 3.2. Groq LLM Client & Fallback (`backend/llm.py`)
```python
class GroqLLM:
    def __init__(self, api_key: str, primary_model="openai/gpt-oss-120b", fallback_model="qwen/qwen3.8-27b"):
        ...
        
    async def chat_completion(self, messages: list, tools: list = None, response_format = None) -> str | dict:
        """
        Executes chat completion with retry logic.
        If tool calling fails or response is malformed, retries once, then gracefully
        falls back to secondary model or structured JSON parser.
        """
```

### 3.3. Incident Agent (`backend/agent.py`)
```python
class IncidentAgent:
    def __init__(self, memory: IncidentMemory, llm: GroqLLM):
        ...
        
    async def analyze_incident(self, incident: Dict, use_memory: bool = True) -> Dict[str, Any]:
        """
        Generates both memory-informed briefing (if use_memory=True and Hindsight healthy)
        and generic zero-memory briefing to support the UI Memory ON/OFF comparison.
        """
        
    async def propose_fix(self, incident_id: str, service: str) -> Dict[str, Any]:
        """Proposes executable command with risk level and human confirmation requirement."""

    async def resolve_with_feedback(self, incident_id: str, resolution_notes: str, feedback: Dict) -> Dict:
        """Closes incident and calls retain_incident to feed memory bank."""
```

### 3.4. REST API & SSE (`backend/main.py`)
- `GET /api/health`: Health status of backend, Groq, and Hindsight (with latency check).
- `GET /api/incidents`: Active and resolved incidents.
- `GET /api/incidents/{id}`: Detailed incident payload including both memory-informed and generic briefings, and raw recalled items.
- `POST /api/incidents/simulate`: Trigger realistic incident or specific demo scenario (Scenario 1, Scenario 5, Scenario 20).
- `POST /api/incidents/{id}/command`: Execute approved mitigation command (e.g. `rollout restart`, `scale`, `config patch`).
- `POST /api/incidents/{id}/resolve`: Close incident with engineer feedback and retain into Hindsight.
- `GET /api/memory/stats`: Current memory bank statistics and recalled incident audit.
- `POST /api/memory/seed`: Trigger automated seeding of `seed_incidents.json`.
- `GET /api/events`: Server-Sent Events stream emitting live metrics (`rps`, `p95`, `err`), new logs, and incident status updates.

---

## 4. Seed Incidents Dataset Design (`data/seed_incidents.json`)
45 rich historical incidents across services:
- `payments`, `postgres`, `redis`, `orders`, `auth`, `api-gateway`, `search`, `ml-ranker`, `queue`.
- **Recurring Patterns with Explicit Failed Fixes**:
  1. **Pattern 1: Payments DB Pool Starvation after Friday Deploys**
     - *Working Fix*: Tune HikariCP `maximumPoolSize=60` and `idleTimeout`, increase Postgres `max_connections`.
     - *Failed Fix*: Restarting `payments` deployment caused severe connection storm crashing Postgres.
  2. **Pattern 2: Redis Eviction Storm during Morning Traffic Spikes**
     - *Working Fix*: Switch policy from `allkeys-lru` to `volatile-lru` and add 4GB memory shard.
     - *Failed Fix*: Running `FLUSHDB` or flushing cache caused complete cache stampede and saturated backend databases.
  3. **Pattern 3: API Gateway 504 Timeout Cascade**
     - *Working Fix*: Circuit-breaker tripping to fast-fail order search fallback.
     - *Failed Fix*: Bumping API gateway read timeout to 60s locked all HTTP connection worker threads.
  4. **Pattern 4: Kafka Dead-Letter Queue Deserialization Panic**
     - *Working Fix*: Hotfix consumer schema registry compatibility level to `FORWARD`.
     - *Failed Fix*: Manually redriving DLQ messages without updating consumer schema resulted in infinite retry loop and disk saturation.

---

## 5. UI Enhancements for Echo-Core (`frontend/sentinelops.html`)
Preserving the exact visual aesthetic, color palettes, and interactive SVG topology while adding:
1. **Brand Update**: "Echo-Core — Autonomous Incident Response with Persistent Memory".
2. **Memory ON/OFF Toggle**:
   - Visual toggle on every expanded incident card:
   - When ON: Shows **Echo-Core Memory-Informed Briefing** (Root Cause, Prior Proven Mitigation, ⚠️ **Failed Fix Warnings**, Previous On-call Engineers).
   - When OFF: Shows **Generic LLM Advice** (standard generic suggestions without historical context).
3. **Memory View Inspector**:
   - Tab or drawer showing:
     - Exact past incidents recalled from Hindsight (`INC-1001`, `INC-1028`, etc.)
     - Why they were retrieved (service matching, semantic similarity of stack trace)
     - Retrieval confidence scores & Hindsight reflection reasoning.
4. **Interactive Demo Scenario Buttons**:
   - Quick launch buttons in UI toolbar:
     - `Demo 1: Novel Alert (Cold Start)`
     - `Demo 5: Similar Recall (Pattern Match)`
     - `Demo 20: Warning on Failed Fix (Star Feature)`
5. **Human-in-the-Loop Confirmation Modal**:
   - Requiring the user to type the service name before executing mutating commands.
6. **Resilience & Edge-case Banners**:
   - Amber banner when Hindsight is offline / unreachable: agent seamlessly degrades to generic mode with an alert notification.
   - Dedup badge when duplicate alerts merge.

---

## 6. Implementation Sequence & Commit Plan

1. **Step 1: Memory Layer & Seed Script**
   - Implement `backend/config.py` and `backend/memory.py` using `hindsight-client`.
   - Implement `data/seed_incidents.json` (45 realistic past incident cases with failed fixes).
   - Implement `scripts/seed_memory.py` to ingest into Hindsight bank `acme-sre`.
   - *Verification*: Run seeding script, verify recall and reflect output against bank.
   - *Commit*: `git commit -m "feat(memory): implement Hindsight wrapper, seed dataset, and ingestion script"`

2. **Step 2: LLM Client & Incident Response Agent**
   - Implement `backend/llm.py` with Groq (`openai/gpt-oss-120b`, fallback `qwen/qwen3.8-27b`) and retry logic.
   - Implement `backend/agent.py` with agent tools (`recall_similar_incidents`, `get_runbook`, `propose_fix`, `run_command`, `retain_outcome`).
   - Implement dual-mode analysis (Memory ON vs Memory OFF).
   - *Verification*: Run unit test running agent against simulated alert.
   - *Commit*: `git commit -m "feat(agent): implement Groq LLM integration, agent tools, and dual-mode briefing"`

3. **Step 3: Simulator & FastAPI Backend**
   - Implement `backend/simulator.py` (telemetry curves, log stream, alert state machine).
   - Implement `backend/main.py` with REST endpoints + SSE stream (`/api/events`).
   - Handle edge cases (empty recall, duplicate alert merging, Hindsight unreachable banner).
   - *Verification*: Start server with `uvicorn`, test API endpoints and SSE stream with curl/python.
   - *Commit*: `git commit -m "feat(backend): implement FastAPI app, simulator, and SSE streaming"`

4. **Step 4: Echo-Core Dashboard Wiring**
   - Connect `frontend/sentinelops.html` to backend REST + SSE endpoints.
   - Rename to **Echo-Core**.
   - Wire Memory ON/OFF toggle, Agent Briefing with ⚠️ Failed Fix warnings, Memory View inspector, and Demo scenario buttons.
   - *Verification*: Open in browser, trigger demo scenario, confirm live updates, commands, resolution retain loop.
   - *Commit*: `git commit -m "feat(frontend): connect Echo-Core dashboard with live SSE, memory toggle, and briefing"`

5. **Step 5: Review, Polish & Documentation**
   - Verify End-to-End Demo Scenario:
     - Incident 1: Generic advice (cold start).
     - Incident 5: Recalls past incident with proven fix.
     - Incident 20: Explicitly detects pattern and warns against disastrous past failed fix!
   - Write comprehensive `README.md` with architecture diagram, Hindsight memory deep-dive, `.env.example`, and quick-start instructions.
   - *Commit*: `git commit -m "docs: add comprehensive README with architecture diagram, Hindsight usage, and demo guide"`

---

## 7. Approval Checkpoint
This plan is ready for review. I will pause here for your confirmation and approval before beginning implementation.
