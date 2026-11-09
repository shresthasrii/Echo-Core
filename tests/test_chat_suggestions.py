import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.database import db

client = TestClient(app)

def test_chat_endpoint():
    res = client.post("/api/chat", json={
        "message": "Payments service is overloading! What should I do?",
        "session_id": "test-chat"
    })
    assert res.status_code == 200
    data = res.json()
    assert "role" in data
    assert data["role"] == "assistant"
    assert "content" in data
    assert len(data["content"]) > 0

    # Verify history
    h_res = client.get("/api/chat/history?session_id=test-chat")
    assert h_res.status_code == 200
    history = h_res.json()["messages"]
    assert len(history) >= 2

def test_suggestion_lifecycle_via_api():
    # 1. Propose suggestion for an incident
    sim_res = client.post("/api/incidents/simulate", json={"service": "payments", "title": "Connection spike"})
    inc = sim_res.json()["incident"]
    inc_id = inc["id"]

    sug_res = client.post(f"/api/incidents/{inc_id}/suggest")
    assert sug_res.status_code == 200
    sug = sug_res.json()
    sug_id = sug["id"]
    assert sug["status"] == "pending"

    # 2. List pending suggestions
    list_res = client.get("/api/suggestions?status=pending")
    assert list_res.status_code == 200
    assert any(s["id"] == sug_id for s in list_res.json())

    # 3. Approve suggestion (execute)
    app_res = client.post(f"/api/suggestions/{sug_id}/approve", json={"actor": "Lead SRE Test"})
    assert app_res.status_code == 200
    res_data = app_res.json()
    assert res_data["success"] is True
    assert res_data["suggestion"]["status"] == "approved"

    # 4. Check audit log
    audit_res = client.get("/api/audit")
    assert audit_res.status_code == 200
    assert any("SUGGESTION_APPROVE" in a["action"] for a in audit_res.json())

def test_database_stats_api():
    res = client.get("/api/database/stats")
    assert res.status_code == 200
    data = res.json()
    assert "total_incidents" in data
    assert "db_location" in data
