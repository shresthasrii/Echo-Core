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

def test_client_report_api():
    payload = {
        "specs": {
            "os": "Windows 11 (64-bit)",
            "cpuCores": 16,
            "deviceRam": "16 GB Physical RAM",
            "gpu": "NVIDIA GeForce RTX 4060 Laptop GPU"
        },
        "metrics": {"cpu": 32, "ram": 48, "disk": 22},
        "checks": [
            {"name": "CPU Responsiveness", "status": "PASSED", "detail": "Test pass"},
            {"name": "Memory Pressure", "status": "WARNING", "detail": "High load"}
        ],
        "overall_health": "WARNING",
        "incidents": [
            {
                "svc": "client-device",
                "sev": 2,
                "title": "Elevated Workstation Load on Windows 11",
                "cause": "Client CPU/RAM load above baseline",
                "error_log": "CLIENT_WARN: Thread event loop lag elevated."
            }
        ],
        "remediations": [
            {
                "action_title": "Recycle Client Tab Resources",
                "command": "ops client purge-cache",
                "risk_level": "LOW",
                "reasoning": "Releases client memory buffers"
            }
        ]
    }
    res = client.post("/api/host/client-report", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert len(data["created_incidents"]) == 1
    assert len(data["staged_suggestions"]) == 1
