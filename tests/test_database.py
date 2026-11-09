import pytest
import asyncio
from backend.database import db

@pytest.mark.asyncio
async def test_database_init_and_stats():
    db.init_db()
    stats = await db.get_db_stats()
    assert "total_incidents" in stats
    assert "audit_logs" in stats
    assert "pending_suggestions" in stats

@pytest.mark.asyncio
async def test_incident_persistence():
    test_inc = {
        "id": "INC-TEST-999",
        "svc": "payments",
        "sev": 1,
        "title": "Database connection pool test",
        "cause": "Synthetic unit test",
        "status": "active",
        "ev": [[1000, "Created test incident"]]
    }
    await db.save_incident(test_inc)
    active = await db.load_active_incidents()
    assert "INC-TEST-999" in active
    assert active["INC-TEST-999"]["svc"] == "payments"

    # Mark resolved
    test_inc["status"] = "resolved"
    test_inc["resolver"] = "Test SRE"
    await db.save_incident(test_inc)
    
    history = await db.get_history_incidents(limit=10)
    assert any(i["id"] == "INC-TEST-999" for i in history)

@pytest.mark.asyncio
async def test_suggestion_approval_lifecycle():
    sug = await db.create_suggestion(
        incident_id="INC-TEST-999",
        svc="payments",
        action_title="Scale PgBouncer",
        recommendation="Prevent pool exhaustion",
        command="kubectl scale deploy/pgbouncer --replicas=3",
        risk_level="LOW",
        reasoning="Proven mitigation in unit test"
    )
    assert sug["id"] is not None
    assert sug["status"] == "pending"

    # Fetch pending
    pending = await db.list_suggestions(status="pending")
    assert any(s["id"] == sug["id"] for s in pending)

    # Approve
    approved = await db.update_suggestion_status(
        sug_id=sug["id"],
        status="approved",
        actor="Lead SRE",
        execution_result="Command succeeded"
    )
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "Lead SRE"

@pytest.mark.asyncio
async def test_chat_message_persistence():
    msg_id = await db.save_chat_message(
        role="user",
        content="How do I mitigate payments load?",
        session_id="test-session"
    )
    assert msg_id > 0
    history = await db.get_chat_history(session_id="test-session")
    assert len(history) >= 1
    assert history[-1]["content"] == "How do I mitigate payments load?"

@pytest.mark.asyncio
async def test_audit_log():
    await db.log_audit(
        action="TEST_ACTION",
        incident_id="INC-TEST-999",
        svc="payments",
        actor="Unit Test Runner",
        details="Verifying audit trail recording"
    )
    logs = await db.get_audit_logs(limit=10)
    assert any(l["action"] == "TEST_ACTION" for l in logs)
