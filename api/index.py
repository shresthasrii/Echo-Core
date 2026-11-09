"""
Vercel Serverless Entrypoint for Echo-Core.
Exposes FastAPI application instance for Vercel Python runtime.
"""
import sys
from pathlib import Path

# Ensure root directory is on Python path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.main import app

# Export for ASGI serverless runners
__all__ = ["app"]
