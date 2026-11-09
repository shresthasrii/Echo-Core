import sqlite3
import json
import time
import os
import logging
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional
from backend.config import settings, BASE_DIR

logger = logging.getLogger("echo_core.database")

_use_in_memory_mode: bool = False
_shared_mem_keepalive: Optional[sqlite3.Connection] = None

def _get_connection() -> sqlite3.Connection:
    """Creates a connection with row factory, with auto-fallback to shared in-memory SQLite if disk fails."""
    global _use_in_memory_mode, _shared_mem_keepalive

    if _use_in_memory_mode:
        if _shared_mem_keepalive is None:
            _shared_mem_keepalive = sqlite3.connect("file:echo_core_shared?mode=memory&cache=shared", uri=True, check_same_thread=False)
        conn = sqlite3.connect("file:echo_core_shared?mode=memory&cache=shared", uri=True, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    db_path = settings.SQLITE_DB_PATH
    try:
        os.makedirs(os.path.dirname(str(db_path)), exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass
        return conn
    except Exception as e:
        logger.warning(f"Failed to open disk database at {db_path}: {e}. Falling back to shared in-memory SQLite.")
        _use_in_memory_mode = True
        if _shared_mem_keepalive is None:
            _shared_mem_keepalive = sqlite3.connect("file:echo_core_shared?mode=memory&cache=shared", uri=True, check_same_thread=False)
        conn = sqlite3.connect("file:echo_core_shared?mode=memory&cache=shared", uri=True, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

class EchoDatabase:
    """
    SQLite persistence layer for Echo-Core.
    Persists:
    - Incidents (active, resolved, briefings, timelines)
    - Alerts (telemetry triggers)
    - Chat history (AI SRE assistant conversation)
    - AI Suggestions (approval-gated mitigation actions: pending -> approved/rejected)
    - Audit Trail (all operator actions, approvals, resolutions)
    """

    def __init__(self):
        self._initialized = False

    def _create_schema(self, conn: sqlite3.Connection):
        """Executes table and index creation on the provided connection."""
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    svc TEXT NOT NULL,
                    sev INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    cause TEXT,
                    error_log TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    briefing_json TEXT,
                    generic_briefing_json TEXT,
                    comparison_json TEXT,
                    recalled_items_json TEXT,
                    events_json TEXT,
                    duplicate_count INTEGER DEFAULT 0,
                    resolver TEXT,
                    fix_worked TEXT,
                    fixes_failed TEXT,
                    is_cascade INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    resolved_at INTEGER
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_svc ON incidents(svc);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT,
                    svc TEXT NOT NULL,
                    sev INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT,
                    timestamp INTEGER NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_inc ON alerts(incident_id);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL DEFAULT 'default',
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    incident_id TEXT,
                    suggestion_id INTEGER,
                    timestamp INTEGER NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, timestamp);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT NOT NULL,
                    svc TEXT NOT NULL,
                    action_title TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    command TEXT NOT NULL,
                    risk_level TEXT NOT NULL DEFAULT 'LOW',
                    reasoning TEXT,
                    status TEXT NOT NULL DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
                    approved_by TEXT,
                    rejected_reason TEXT,
                    execution_result TEXT,
                    created_at INTEGER NOT NULL,
                    acted_at INTEGER
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_status ON ai_suggestions(status);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    incident_id TEXT,
                    svc TEXT,
                    actor TEXT NOT NULL DEFAULT 'On-Call SRE',
                    details TEXT,
                    timestamp INTEGER NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp DESC);")

    def init_db(self):
        """Initializes tables and indexes synchronously, with seamless fallback to shared in-memory DB if disk is read-only."""
        global _use_in_memory_mode, _shared_mem_keepalive

        # 1. Try configured disk path
        if not _use_in_memory_mode:
            try:
                conn = _get_connection()
                try:
                    self._create_schema(conn)
                    self._initialized = True
                    logger.info(f"Echo-Core SQLite database initialized successfully at {settings.SQLITE_DB_PATH}.")
                    return
                finally:
                    conn.close()
            except Exception as disk_err:
                logger.warning(f"Failed to initialize schema on disk ({disk_err}). Switching to shared in-memory SQLite.")
                _use_in_memory_mode = True

        # 2. Shared in-memory fallback
        try:
            if _shared_mem_keepalive is None:
                _shared_mem_keepalive = sqlite3.connect("file:echo_core_shared?mode=memory&cache=shared", uri=True, check_same_thread=False)
            conn = sqlite3.connect("file:echo_core_shared?mode=memory&cache=shared", uri=True, timeout=10.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                self._create_schema(conn)
                self._initialized = True
                logger.info("Echo-Core shared in-memory SQLite database initialized successfully.")
            finally:
                conn.close()
        except Exception as mem_err:
            logger.error(f"Failed to initialize in-memory SQLite database: {mem_err}")

    # --- INCIDENTS ---
    def save_incident_sync(self, inc: Dict[str, Any]):
        conn = _get_connection()
        try:
            with conn:
                briefing_json = json.dumps(inc.get("briefing"), default=str) if inc.get("briefing") else None
                generic_briefing_json = json.dumps(inc.get("generic_briefing"), default=str) if inc.get("generic_briefing") else None
                comparison_json = json.dumps(inc.get("comparison"), default=str) if inc.get("comparison") else None
                recalled_items_json = json.dumps(inc.get("recalled_items"), default=str) if inc.get("recalled_items") else None
                events_json = json.dumps(inc.get("ev", []), default=str)
                
                conn.execute("""
                    INSERT INTO incidents (
                        id, svc, sev, title, cause, error_log, status,
                        briefing_json, generic_briefing_json, comparison_json,
                        recalled_items_json, events_json, duplicate_count,
                        resolver, fix_worked, fixes_failed, is_cascade,
                        created_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        sev=excluded.sev,
                        title=excluded.title,
                        cause=excluded.cause,
                        error_log=excluded.error_log,
                        status=excluded.status,
                        briefing_json=COALESCE(excluded.briefing_json, incidents.briefing_json),
                        generic_briefing_json=COALESCE(excluded.generic_briefing_json, incidents.generic_briefing_json),
                        comparison_json=COALESCE(excluded.comparison_json, incidents.comparison_json),
                        recalled_items_json=COALESCE(excluded.recalled_items_json, incidents.recalled_items_json),
                        events_json=excluded.events_json,
                        duplicate_count=excluded.duplicate_count,
                        resolver=COALESCE(excluded.resolver, incidents.resolver),
                        fix_worked=COALESCE(excluded.fix_worked, incidents.fix_worked),
                        fixes_failed=COALESCE(excluded.fixes_failed, incidents.fixes_failed),
                        resolved_at=COALESCE(excluded.resolved_at, incidents.resolved_at);
                """, (
                    inc["id"],
                    inc.get("svc", "unknown"),
                    inc.get("sev", 3),
                    inc.get("title", ""),
                    inc.get("cause", ""),
                    inc.get("error_log", ""),
                    inc.get("status", "active"),
                    briefing_json,
                    generic_briefing_json,
                    comparison_json,
                    recalled_items_json,
                    events_json,
                    inc.get("duplicate_count", 0),
                    inc.get("resolver"),
                    inc.get("fix_worked"),
                    str(inc.get("fixes_failed", "")) if inc.get("fixes_failed") else None,
                    1 if inc.get("is_cascade") else 0,
                    inc.get("t", int(time.time() * 1000)),
                    int(time.time() * 1000) if inc.get("status") == "resolved" else None
                ))
        finally:
            conn.close()

    async def save_incident(self, inc: Dict[str, Any]):
        await asyncio.to_thread(self.save_incident_sync, inc)

    def load_active_incidents_sync(self) -> Dict[str, Dict[str, Any]]:
        conn = _get_connection()
        result = {}
        try:
            cursor = conn.execute("SELECT * FROM incidents WHERE status = 'active' ORDER BY created_at DESC;")
            for row in cursor.fetchall():
                inc = dict(row)
                inc["t"] = inc["created_at"]
                inc["ev"] = json.loads(inc["events_json"]) if inc.get("events_json") else []
                if inc.get("briefing_json"):
                    inc["briefing"] = json.loads(inc["briefing_json"])
                if inc.get("generic_briefing_json"):
                    inc["generic_briefing"] = json.loads(inc["generic_briefing_json"])
                if inc.get("comparison_json"):
                    inc["comparison"] = json.loads(inc["comparison_json"])
                if inc.get("recalled_items_json"):
                    inc["recalled_items"] = json.loads(inc["recalled_items_json"])
                inc["is_cascade"] = bool(inc.get("is_cascade"))
                result[inc["id"]] = inc
        finally:
            conn.close()
        return result

    async def load_active_incidents(self) -> Dict[str, Dict[str, Any]]:
        return await asyncio.to_thread(self.load_active_incidents_sync)

    def get_history_incidents_sync(self, limit: int = 50) -> List[Dict[str, Any]]:
        conn = _get_connection()
        history = []
        try:
            cursor = conn.execute("""
                SELECT * FROM incidents 
                WHERE status = 'resolved' 
                ORDER BY resolved_at DESC LIMIT ?;
            """, (limit,))
            for row in cursor.fetchall():
                inc = dict(row)
                inc["t"] = inc["created_at"]
                inc["ev"] = json.loads(inc["events_json"]) if inc.get("events_json") else []
                if inc.get("briefing_json"):
                    inc["briefing"] = json.loads(inc["briefing_json"])
                history.append(inc)
        finally:
            conn.close()
        return history

    async def get_history_incidents(self, limit: int = 50) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_history_incidents_sync, limit)

    # --- ALERTS ---
    def save_alert_sync(self, incident_id: str, svc: str, sev: int, title: str, message: str, timestamp: int):
        conn = _get_connection()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO alerts (incident_id, svc, sev, title, message, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?);
                """, (incident_id, svc, sev, title, message, timestamp))
        finally:
            conn.close()

    async def save_alert(self, incident_id: str, svc: str, sev: int, title: str, message: str, timestamp: Optional[int] = None):
        ts = timestamp or int(time.time() * 1000)
        await asyncio.to_thread(self.save_alert_sync, incident_id, svc, sev, title, message, ts)

    def get_alerts_sync(self, limit: int = 50) -> List[Dict[str, Any]]:
        conn = _get_connection()
        alerts = []
        try:
            cursor = conn.execute("SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?;", (limit,))
            alerts = [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
        return alerts

    async def get_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_alerts_sync, limit)

    # --- CHAT MESSAGES ---
    def save_chat_message_sync(self, role: str, content: str, incident_id: Optional[str] = None, suggestion_id: Optional[int] = None, session_id: str = "default") -> int:
        conn = _get_connection()
        try:
            with conn:
                cursor = conn.execute("""
                    INSERT INTO chat_messages (session_id, role, content, incident_id, suggestion_id, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?);
                """, (session_id, role, content, incident_id, suggestion_id, int(time.time() * 1000)))
                return cursor.lastrowid
        finally:
            conn.close()

    async def save_chat_message(self, role: str, content: str, incident_id: Optional[str] = None, suggestion_id: Optional[int] = None, session_id: str = "default") -> int:
        return await asyncio.to_thread(self.save_chat_message_sync, role, content, incident_id, suggestion_id, session_id)

    def get_chat_history_sync(self, session_id: str = "default", limit: int = 50) -> List[Dict[str, Any]]:
        conn = _get_connection()
        messages = []
        try:
            cursor = conn.execute("""
                SELECT cm.*, 
                       s.id as sug_id, s.action_title, s.recommendation, s.command, s.risk_level, s.status as sug_status, s.reasoning
                FROM chat_messages cm
                LEFT JOIN ai_suggestions s ON cm.suggestion_id = s.id
                WHERE cm.session_id = ?
                ORDER BY cm.timestamp ASC LIMIT ?;
            """, (session_id, limit))
            for row in cursor.fetchall():
                d = dict(row)
                if d.get("suggestion_id") and d.get("action_title"):
                    d["suggestion"] = {
                        "id": d["sug_id"],
                        "incident_id": d["incident_id"],
                        "action_title": d["action_title"],
                        "recommendation": d["recommendation"],
                        "command": d["command"],
                        "risk_level": d["risk_level"],
                        "status": d["sug_status"],
                        "reasoning": d["reasoning"]
                    }
                messages.append(d)
        finally:
            conn.close()
        return messages

    async def get_chat_history(self, session_id: str = "default", limit: int = 50) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_chat_history_sync, session_id, limit)

    def clear_chat_history_sync(self, session_id: str = "default"):
        conn = _get_connection()
        try:
            with conn:
                conn.execute("DELETE FROM chat_messages WHERE session_id = ?;", (session_id,))
        finally:
            conn.close()

    async def clear_chat_history(self, session_id: str = "default"):
        await asyncio.to_thread(self.clear_chat_history_sync, session_id)

    # --- AI SUGGESTIONS (APPROVAL GATE SYSTEM) ---
    def create_suggestion_sync(
        self,
        incident_id: str,
        svc: str,
        action_title: str,
        recommendation: str,
        command: str,
        risk_level: str = "LOW",
        reasoning: Optional[str] = None
    ) -> Dict[str, Any]:
        conn = _get_connection()
        try:
            now = int(time.time() * 1000)
            with conn:
                cursor = conn.execute("""
                    INSERT INTO ai_suggestions (
                        incident_id, svc, action_title, recommendation, command, risk_level, reasoning, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?);
                """, (incident_id, svc, action_title, recommendation, command, risk_level, reasoning, now))
                sug_id = cursor.lastrowid
                return {
                    "id": sug_id,
                    "incident_id": incident_id,
                    "svc": svc,
                    "action_title": action_title,
                    "recommendation": recommendation,
                    "command": command,
                    "risk_level": risk_level,
                    "reasoning": reasoning,
                    "status": "pending",
                    "created_at": now
                }
        finally:
            conn.close()

    async def create_suggestion(
        self,
        incident_id: str,
        svc: str,
        action_title: str,
        recommendation: str,
        command: str,
        risk_level: str = "LOW",
        reasoning: Optional[str] = None
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.create_suggestion_sync,
            incident_id, svc, action_title, recommendation, command, risk_level, reasoning
        )

    def get_suggestion_sync(self, sug_id: int) -> Optional[Dict[str, Any]]:
        conn = _get_connection()
        try:
            cursor = conn.execute("SELECT * FROM ai_suggestions WHERE id = ?;", (sug_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_suggestion(self, sug_id: int) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_suggestion_sync, sug_id)

    def list_suggestions_sync(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        conn = _get_connection()
        try:
            if status:
                cursor = conn.execute(
                    "SELECT * FROM ai_suggestions WHERE status = ? ORDER BY created_at DESC LIMIT ?;",
                    (status, limit)
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM ai_suggestions ORDER BY created_at DESC LIMIT ?;",
                    (limit,)
                )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    async def list_suggestions(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_suggestions_sync, status, limit)

    def update_suggestion_status_sync(
        self,
        sug_id: int,
        status: str,
        actor: Optional[str] = None,
        rejected_reason: Optional[str] = None,
        execution_result: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        conn = _get_connection()
        try:
            now = int(time.time() * 1000)
            with conn:
                conn.execute("""
                    UPDATE ai_suggestions SET
                        status = ?,
                        approved_by = COALESCE(?, approved_by),
                        rejected_reason = COALESCE(?, rejected_reason),
                        execution_result = COALESCE(?, execution_result),
                        acted_at = ?
                    WHERE id = ?;
                """, (status, actor, rejected_reason, execution_result, now, sug_id))
            return self.get_suggestion_sync(sug_id)
        finally:
            conn.close()

    async def update_suggestion_status(
        self,
        sug_id: int,
        status: str,
        actor: Optional[str] = None,
        rejected_reason: Optional[str] = None,
        execution_result: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(
            self.update_suggestion_status_sync,
            sug_id, status, actor, rejected_reason, execution_result
        )

    # --- AUDIT LOG ---
    def log_audit_sync(
        self,
        action: str,
        incident_id: Optional[str] = None,
        svc: Optional[str] = None,
        actor: str = "On-Call SRE",
        details: str = ""
    ):
        conn = _get_connection()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO audit_log (action, incident_id, svc, actor, details, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?);
                """, (action, incident_id, svc, actor, details, int(time.time() * 1000)))
        finally:
            conn.close()

    async def log_audit(
        self,
        action: str,
        incident_id: Optional[str] = None,
        svc: Optional[str] = None,
        actor: str = "On-Call SRE",
        details: str = ""
    ):
        await asyncio.to_thread(self.log_audit_sync, action, incident_id, svc, actor, details)

    def get_audit_logs_sync(self, limit: int = 100) -> List[Dict[str, Any]]:
        conn = _get_connection()
        try:
            cursor = conn.execute("SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?;", (limit,))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    async def get_audit_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_audit_logs_sync, limit)

    # --- STATS ---
    def get_db_stats_sync(self) -> Dict[str, Any]:
        conn = _get_connection()
        try:
            c_inc = conn.execute("SELECT COUNT(*) FROM incidents;").fetchone()[0]
            c_active = conn.execute("SELECT COUNT(*) FROM incidents WHERE status='active';").fetchone()[0]
            c_res = conn.execute("SELECT COUNT(*) FROM incidents WHERE status='resolved';").fetchone()[0]
            c_alerts = conn.execute("SELECT COUNT(*) FROM alerts;").fetchone()[0]
            c_chat = conn.execute("SELECT COUNT(*) FROM chat_messages;").fetchone()[0]
            c_sug_pending = conn.execute("SELECT COUNT(*) FROM ai_suggestions WHERE status='pending';").fetchone()[0]
            c_sug_total = conn.execute("SELECT COUNT(*) FROM ai_suggestions;").fetchone()[0]
            c_audit = conn.execute("SELECT COUNT(*) FROM audit_log;").fetchone()[0]
            return {
                "total_incidents": c_inc,
                "active_incidents": c_active,
                "resolved_incidents": c_res,
                "total_alerts": c_alerts,
                "chat_messages": c_chat,
                "pending_suggestions": c_sug_pending,
                "total_suggestions": c_sug_total,
                "audit_logs": c_audit,
                "db_location": "in-memory (shared cache)" if _use_in_memory_mode else str(settings.SQLITE_DB_PATH)
            }
        finally:
            conn.close()

    async def get_db_stats(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self.get_db_stats_sync)

# Global singleton database instance
db = EchoDatabase()
