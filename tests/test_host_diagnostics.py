import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.host_scanner import host_scanner

client = TestClient(app)

def test_host_scanner_specs():
    specs = host_scanner.get_host_specs()
    assert "os" in specs
    assert "hostname" in specs
    assert "cpu_cores" in specs
    assert specs["cpu_cores"] >= 1

def test_host_live_metrics_api():
    res = client.get("/api/host/metrics")
    assert res.status_code == 200
    data = res.json()
    assert "specs" in data
    assert "cpu_percent" in data
    assert "ram_percent" in data
    assert "disk_percent" in data
    assert isinstance(data["top_processes"], list)

def test_host_diagnostic_benchmark_api():
    res = client.post("/api/host/diagnostic-test")
    assert res.status_code == 200
    report = res.json()
    assert "overall_health" in report
    assert "checks" in report
    assert len(report["checks"]) >= 4
    assert "created_incidents" in report
    assert "staged_suggestions" in report
