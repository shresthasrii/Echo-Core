import time
import random
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger("echo_core.simulator")

SERVICES = [
    "api-gateway", "auth", "orders", "search",
    "payments", "ml-ranker", "postgres", "redis", "queue"
]

LOG_TEMPLATES = {
    "INFO": [
        "request completed in {n}ms (status: 200)",
        "cache hit ratio at 96.4% on shard {n}",
        "health probe /healthz returned UP for pod replica {n}",
        "deploy v{n} canary traffic shifted successfully",
        "OAuth2 token verified for tenant-org-{n}"
    ],
    "WARN": [
        "slow query detected ({n}ms) on orders_by_customer_idx",
        "retrying upstream connection to auth.internal (attempt 2)",
        "connection pool utilization above 82% on payments-db",
        "p95 latency exceeded 220ms threshold over 60s window",
        "consumer group lag growing on topic events-{n}"
    ],
    "ERROR": [
        "upstream timeout after 3000ms waiting for response header",
        "failed to acquire connection from pool within 5000ms",
        "unhandled NullPointerException in checkout transaction handler",
        "HTTP 503 Service Unavailable returned to edge gateway",
        "dead-letter queue consumer failed with serialization exception"
    ]
}

DEMO_SCENARIOS = {
    1: {
        "svc": "ml-ranker",
        "sev": 3,
        "title": "Unusual GPU threadpool memory fragmentation",
        "cause": "Dynamic tensor allocations causing resident set size (RSS) bloat on newly deployed PyTorch model.",
        "error_log": "CUDA worker process memory leak: RSS 15.8GB / 16.0GB. No historical runbook entry found.",
        "scenario_type": "cold_start",
        "label": "Demo 1: Novel Alert (Cold Start / Generic Advice)"
    },
    5: {
        "svc": "auth",
        "sev": 2,
        "title": "Redis OOM eviction storm causing session validation dropouts",
        "cause": "Session tokens stored without TTL expiration caused Redis memory to hit the 8GB maxmemory ceiling.",
        "error_log": "OOM command not allowed when used memory > 'maxmemory' (used: 8192MB / 8192MB) on redis-auth",
        "scenario_type": "pattern_match",
        "label": "Demo 5: Similar Recall (Redis Eviction Storm)"
    },
    20: {
        "svc": "payments",
        "sev": 1,
        "title": "Postgres connection pool exhaustion during post-deploy traffic spike",
        "cause": "HikariCP default connection pool limit was exceeded when checkout webhook traffic surged after Friday deploy.",
        "error_log": "FATAL: remaining connection slots are reserved for non-replication superuser connections (active: 100/100)",
        "scenario_type": "failed_fix_warning",
        "label": "Demo 20: Star Feature (Critical Warning on Previously Failed Fix)"
    }
}

class TelemetrySimulator:
    """
    Simulates real-time system metrics (RPS, p95 latency, error rate),
    live incident state machine, multi-service cascading failures, and log generation.
    """
    def __init__(self):
        self.rps: float = 4200.0
        self.p95: float = 145.0
        self.err: float = 0.28
        self.history: Dict[str, List[float]] = {
            "rps": [], "p95": [], "err": [], "inc": []
        }
        self.incidents: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.seq: int = 2040
        self.is_paused: bool = False
        
        for _ in range(30):
            self.tick_metrics()
            
        self._init_default_incidents()

    def _init_default_incidents(self):
        t_now = int(time.time() * 1000)
        init_cases = [
            ("payments", 2, "Elevated 5xx rate on checkout API", "Upstream timeouts began after the latest deploy; client retries are amplifying load.", t_now - 250000),
            ("postgres", 3, "Replica replication lag rising", "Replication delay keeps rising, so read-replicas may return stale catalog data.", t_now - 610000),
            ("search", 4, "Cache hit ratio dropped on product search", "An eviction storm followed the key schema change.", t_now - 180000)
        ]
        for svc, sev, title, cause, t in init_cases:
            self.seq += 1
            inc_id = f"INC-{self.seq}"
            self.incidents[inc_id] = {
                "id": inc_id,
                "svc": svc,
                "sev": sev,
                "title": title,
                "cause": cause,
                "t": t,
                "ev": [[t, f"Alert raised: Severity {sev}"]],
                "duplicate_count": 0,
                "status": "active"
            }

    def tick_metrics(self):
        if self.is_paused:
            return
            
        load = sum(5 - inc["sev"] for inc in self.incidents.values() if inc.get("status") == "active")
        
        self.rps = max(2400.0, min(6500.0, self.rps + random.uniform(-90, 90) - (load * 6)))
        self.p95 = max(90.0, self.p95 + (135.0 + load * 18 - self.p95) * 0.12 + random.uniform(-6, 6))
        self.err = max(0.05, min(15.0, self.err + (0.2 + load * 0.25 - self.err) * 0.15 + random.uniform(-0.03, 0.03)))
        
        self.history["rps"].append(round(self.rps, 1))
        self.history["p95"].append(round(self.p95, 1))
        self.history["err"].append(round(self.err, 2))
        self.history["inc"].append(len([i for i in self.incidents.values() if i.get("status") == "active"]))
        
        for k in self.history:
            if len(self.history[k]) > 32:
                self.history[k].pop(0)

    def generate_log(self) -> Dict[str, Any]:
        active_svcs = [i["svc"] for i in self.incidents.values() if i.get("status") == "active"]
        hot = active_svcs and random.random() < 0.45
        svc = random.choice(active_svcs) if hot else random.choice(SERVICES)
        
        r = random.random()
        level = "ERROR" if (hot and r < 0.5) or (not hot and r < 0.08) else \
                "WARN" if (hot and r < 0.8) or (not hot and r < 0.25) else "INFO"
                
        tmpl = random.choice(LOG_TEMPLATES[level])
        msg = tmpl.replace("{n}", str(random.randint(12, 850)))
        
        log_entry = {
            "t": int(time.time() * 1000),
            "l": level,
            "s": svc,
            "m": msg
        }
        self.logs.insert(0, log_entry)
        if len(self.logs) > 300:
            self.logs.pop()
        return log_entry

    def trigger_incident(
        self,
        svc: Optional[str] = None,
        sev: Optional[int] = None,
        title: Optional[str] = None,
        cause: Optional[str] = None,
        error_log: Optional[str] = None,
        scenario_num: Optional[int] = None
    ) -> Dict[str, Any]:
        if scenario_num in DEMO_SCENARIOS:
            sc = DEMO_SCENARIOS[scenario_num]
            svc = sc["svc"]
            sev = sc["sev"]
            title = sc["title"]
            cause = sc["cause"]
            error_log = sc["error_log"]

        svc = svc or random.choice(SERVICES)
        sev = sev or random.choice([1, 2, 2, 3, 3, 4])
        title = title or f"Elevated latency and connection stalls on {svc}"
        cause = cause or f"Upstream dependency degradation observed on {svc}."
        t_now = int(time.time() * 1000)

        # Merge duplicate alerts on same service
        for existing in self.incidents.values():
            if existing["svc"] == svc and existing["title"] == title and existing.get("status") == "active":
                existing["duplicate_count"] += 1
                existing["sev"] = min(existing["sev"], sev)
                existing["ev"].append([t_now, f"Merged duplicate alert (occurrences: {existing['duplicate_count'] + 1})"])
                logger.info(f"Merged duplicate alert for {existing['id']} on {svc}")
                return {
                    "incident": existing,
                    "merged": True
                }

        self.seq += 1
        inc_id = f"INC-{self.seq}"
        new_inc = {
            "id": inc_id,
            "svc": svc,
            "sev": sev,
            "title": title,
            "cause": cause,
            "error_log": error_log or f"Alert triggered on {svc}: {title}",
            "t": t_now,
            "ev": [[t_now, f"Alert raised: Severity {sev}"]],
            "duplicate_count": 0,
            "status": "active"
        }
        self.incidents[inc_id] = new_inc
        logger.info(f"Created incident {inc_id} on {svc}")
        return {
            "incident": new_inc,
            "merged": False
        }

    def trigger_cascade(self) -> List[Dict[str, Any]]:
        """
        Triggers multi-service cascading outage across dependency topology:
        payments -> postgres -> orders -> api-gateway.
        """
        t_now = int(time.time() * 1000)
        cascade_chain = [
            ("payments", 1, "Payments DB Connection Pool Exhaustion (Cascade Root)", "HikariCP pool starvation following Friday deploy v2.4.1. Exhausting database connection limit.", "FATAL: remaining connection slots are reserved for non-replication superuser connections (active: 100/100)"),
            ("postgres", 2, "Database shared transaction lock lockup", "Downstream locks queuing behind uncommitted payments checkout transaction.", "WARNING: 42 transactions waiting for ExclusiveLock on orders table"),
            ("orders", 2, "Order placement timeouts cascading to upstream", "Checkout RPCs to payments service timed out after 5000ms.", "java.net.SocketTimeoutException: Read timed out to payments.prod:8443"),
            ("api-gateway", 1, "504 Gateway Timeout storm on checkout ingress", "Edge worker threads saturated waiting for order completion.", "upstream timed out (110: Connection timed out) while reading response header")
        ]
        created = []
        for svc, sev, title, cause, error_log in cascade_chain:
            self.seq += 1
            inc_id = f"INC-{self.seq}"
            inc = {
                "id": inc_id,
                "svc": svc,
                "sev": sev,
                "title": title,
                "cause": cause,
                "error_log": error_log,
                "t": t_now,
                "ev": [[t_now, f"Cascading alert raised: Severity {sev}"]],
                "duplicate_count": 0,
                "status": "active",
                "is_cascade": True
            }
            self.incidents[inc_id] = inc
            created.append(inc)
            
        self.p95 = 520.0
        self.err = 6.4
        return created

    def execute_command(self, inc_id: str, cmd_key: str, custom_cmd: Optional[str] = None) -> Dict[str, Any]:
        inc = self.incidents.get(inc_id)
        if not inc:
            return {"success": False, "error": f"Incident {inc_id} not found"}

        t_now = int(time.time() * 1000)
        svc = inc["svc"]
        
        # Check known disastrous anti-patterns
        if svc == "payments" and "restart" in cmd_key.lower():
            inc["sev"] = 1
            inc["ev"].append([t_now, "FATAL: 'kubectl rollout restart' triggered connection storm on Postgres! Severity locked to CRITICAL."])
            self.err += 4.5
            self.p95 += 350.0
            return {
                "success": False,
                "incident": inc,
                "catastrophic": True,
                "message": "DISASTROUS COMMAND: Pod restart caused immediate connection stampede on Postgres! Memory warned against this."
            }

        if svc == "redis" and "flush" in (custom_cmd or cmd_key).lower():
            inc["sev"] = 1
            inc["ev"].append([t_now, "FATAL: Cache flush triggered massive database stampede! CPU saturated at 100%."])
            self.err += 7.0
            self.p95 += 400.0
            return {
                "success": False,
                "incident": inc,
                "catastrophic": True,
                "message": "DISASTROUS COMMAND: Flushing Redis cache triggered complete cache stampede on Postgres!"
            }
            
        if cmd_key == "resolve":
            inc["status"] = "resolved"
            inc["ev"].append([t_now, "Incident marked as resolved by engineer."])
            return {
                "success": True,
                "incident": inc,
                "resolved": True,
                "message": f"Incident {inc_id} resolved."
            }

        if inc["sev"] < 4:
            inc["sev"] += 1
            inc["ev"].append([t_now, f"Mitigation command '{cmd_key}' applied successfully. Severity eased to {inc['sev']}."])
            return {
                "success": True,
                "incident": inc,
                "resolved": False,
                "message": f"Command succeeded. Severity eased to {inc['sev']}."
            }
        else:
            inc["status"] = "resolved"
            inc["ev"].append([t_now, f"Final mitigation '{cmd_key}' completed. Service health recovered."])
            return {
                "success": True,
                "incident": inc,
                "resolved": True,
                "message": f"Incident {inc_id} fully resolved."
            }

# Global simulator instance
sim = TelemetrySimulator()
