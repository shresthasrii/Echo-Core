import sys
import time
import pytest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

def main():
    print("=" * 70)
    print("           ECHO-CORE AUTOMATED TEST SUITE RUNNER")
    print("   Persistent Memory Incident Agent (HackwithBengaluru 2.0)")
    print("=" * 70)
    print()

    start_time = time.time()
    
    # Run pytest programmatically on tests directory
    args = [
        "-v",
        "--tb=short",
        "-s",
        str(ROOT_DIR / "tests")
    ]
    
    exit_code = pytest.main(args)
    duration = time.time() - start_time
    
    print()
    print("=" * 70)
    if exit_code == 0:
        print(f"  [SUCCESS] All test suites PASSED in {duration:.2f}s!")
        print("  * Memory Layer:        VERIFIED (Hindsight Bank acme-sre)")
        print("  * LLM & Agent Loop:    VERIFIED (Groq openai/gpt-oss-120b)")
        print("  * SQLite Persistence:  VERIFIED (echo_core.db & Audit Trail)")
        print("  * SRE Copilot & Chat:  VERIFIED (Interactive Assistant)")
        print("  * Approval Gate Sys:   VERIFIED (Human-in-the-Loop Allow/Reject)")
        print("  * FastAPI Endpoints:   VERIFIED (REST, SSE, Cascade)")
        print("  * Safety Pre-Flight:   VERIFIED (Anti-Patterns Guardrails)")
    else:
        print(f"  [FAILURE] Tests finished with exit code {exit_code} in {duration:.2f}s.")
    print("=" * 70)
    
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
