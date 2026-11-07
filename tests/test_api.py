import pytest
from fastapi.testclient import TestClient
from backend.main import app

client = TestClient(app)

def test_api_health():
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert "hindsight" in data
    assert data["hindsight"]["is_healthy"] is True

def test_api_list_incidents():
    res = client.get("/api/incidents")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) > 0

def test_api_simulate_demo_scenarios():
    # Demo Scenario 1 (Cold Start)
    res1 = client.post("/api/incidents/simulate", json={"scenario_num": 1})
    assert res1.status_code == 200
    d1 = res1.json()["incident"]
    assert d1["svc"] == "ml-ranker"
    assert "briefing" in d1

    # Demo Scenario 5 (Pattern Match)
    res5 = client.post("/api/incidents/simulate", json={"scenario_num": 5})
    assert res5.status_code == 200
    d5 = res5.json()["incident"]
    assert d5["svc"] == "auth"
    assert "briefing" in d5

    # Demo Scenario 20 (Failed Fix Warning)
    res20 = client.post("/api/incidents/simulate", json={"scenario_num": 20})
    assert res20.status_code == 200
    d20 = res20.json()["incident"]
    assert d20["svc"] == "payments"
    assert "failed_fixes" in d20["briefing"]

def test_api_command_execution():
    # Simulate an incident on redis
    sim_res = client.post("/api/incidents/simulate", json={"service": "redis", "title": "High memory usage"})
    inc = sim_res.json()["incident"]
    inc_id = inc["id"]

    # Command with mismatched service confirm should fail with 400
    bad_req = client.post(f"/api/incidents/{inc_id}/command", json={
        "command_key": "scale",
        "service_confirm": "wrong-service"
    })
    assert bad_req.status_code == 400

    # Command with valid confirmation
    good_req = client.post(f"/api/incidents/{inc_id}/command", json={
        "command_key": "scale",
        "command_str": "kubectl scale deploy/redis --replicas=6",
        "service_confirm": "redis"
    })
    assert good_req.status_code == 200
    assert good_req.json()["success"] is True

def test_api_disastrous_restart_simulation():
    # Test disastrous restart on payments
    sim_res = client.post("/api/incidents/simulate", json={"scenario_num": 20})
    inc_id = sim_res.json()["incident"]["id"]

    cmd_res = client.post(f"/api/incidents/{inc_id}/command", json={
        "command_key": "restart",
        "command_str": "kubectl rollout restart deploy/payments",
        "service_confirm": "payments"
    })
    assert cmd_res.status_code == 200
    d = cmd_res.json()
    assert d.get("catastrophic") is True
    assert "DISASTROUS COMMAND" in d.get("message")

def test_api_postmortem_generation():
    sim_res = client.post("/api/incidents/simulate", json={"scenario_num": 5})
    inc_id = sim_res.json()["incident"]["id"]

    res = client.get(f"/api/incidents/{inc_id}/postmortem")
    assert res.status_code == 200
    text = res.text
    assert "# Incident Post-Mortem:" in text
    assert "Executive Summary" in text
    assert "Remediation & Action Taken" in text

def test_api_anti_patterns():
    res = client.get("/api/memory/anti-patterns")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) >= 30
