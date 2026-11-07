import asyncio
from backend.memory import memory
from backend.config import settings

def test_memory_health_and_bank():
    """Verifies that Hindsight client is healthy and bank exists."""
    stats = asyncio.run(memory.get_stats())
    assert stats["bank_id"] == settings.HINDSIGHT_BANK_ID
    assert stats["is_healthy"] is True
    assert stats["documented_anti_patterns"] > 0

def test_memory_anti_patterns():
    """Verifies that anti-patterns catalog returns populated rules."""
    anti_patterns = asyncio.run(memory.get_anti_patterns())
    assert len(anti_patterns) >= 30
    
    # Check that payments pod restart anti-pattern is present
    payments_aps = [ap for ap in anti_patterns if ap["service"] == "payments"]
    assert len(payments_aps) > 0
    assert any("restart" in ap["dangerous_action"].lower() for ap in payments_aps)

def test_memory_search():
    """Verifies memory search query against Hindsight bank."""
    results = asyncio.run(memory.search_memories("connection pool"))
    assert isinstance(results, list)
    assert len(results) > 0
    assert any("pool" in r.get("text", "").lower() or "connection" in r.get("text", "").lower() for r in results)

def test_memory_recall_caching():
    """Verifies that recall returns results and caches them."""
    res1 = asyncio.run(memory.recall_for_alert("payments", "Postgres connection pool"))
    assert isinstance(res1, list)
    assert len(res1) > 0
    
    # Second call should be instant from cache
    res2 = asyncio.run(memory.recall_for_alert("payments", "Postgres connection pool"))
    assert len(res2) == len(res1)
