import asyncio
from backend.agent import agent

def test_runbook_retrieval():
    """Verifies runbook procedures exist for all core services."""
    for svc in ["payments", "redis", "postgres", "api-gateway", "auth", "orders"]:
        rb = agent.get_runbook(svc)
        assert "procedures" in rb
        assert len(rb["procedures"]) > 0

def test_check_command_safety_dangerous():
    """Verifies that dangerous restart command on payments is flagged."""
    check = asyncio.run(agent.check_command_safety("payments", "kubectl rollout restart deploy/payments"))
    assert check["is_dangerous"] is True
    assert "restart" in check["warning"].lower()
    assert "safe_alternative" in check

def test_check_command_safety_safe():
    """Verifies that non-dangerous scale command is not flagged."""
    check = asyncio.run(agent.check_command_safety("payments", "kubectl scale deploy/payments --replicas=6"))
    assert check["is_dangerous"] is False

def test_agent_analyze_incident_dual_briefing():
    """Verifies agent produces both memory-informed and generic briefings."""
    inc = {
        "id": "INC-TEST-1",
        "svc": "payments",
        "sev": 1,
        "title": "Postgres connection pool exhaustion",
        "cause": "HikariCP pool limit hit during deploy."
    }
    analysis = asyncio.run(agent.analyze_incident(inc))
    assert "memory_informed" in analysis
    assert "generic" in analysis
    assert "comparison" in analysis
    
    mem = analysis["memory_informed"]
    gen = analysis["generic"]
    
    assert mem["memory_active"] is True
    assert gen["memory_active"] is False
    assert "likely_cause" in mem
    assert "failed_fixes" in mem
    assert "likely_cause" in gen
