"""
Vercel Serverless Entrypoint for Echo-Core.
Exposes crash-shielded FastAPI application instance for Vercel Python runtime.
"""
import sys
import traceback
from pathlib import Path

# Ensure root directory is on Python path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from backend.main import app as _real_app
    _startup_error = None
except Exception:
    _startup_error = traceback.format_exc()
    print(f"CRITICAL MODULE IMPORT ERROR:\n{_startup_error}", file=sys.stderr)
    _real_app = None

async def app(scope, receive, send):
    """Crash-shielded ASGI entrypoint."""
    if _startup_error is not None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                    break
            return
        if scope["type"] == "http":
            html = f"""<!DOCTYPE html>
<html><head><title>Echo-Core Diagnostic</title>
<style>body{{background:#0d1117;color:#c9d1d9;font-family:sans-serif;padding:32px;}}
pre{{background:#161b22;padding:16px;border-radius:8px;color:#f85149;overflow:auto;}}</style>
</head><body>
<h2>Echo-Core Startup Diagnostic Error</h2>
<p>An uncaught error occurred while importing backend modules:</p>
<pre>{_startup_error}</pre>
</body></html>"""
            body = html.encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("utf-8"))
                ]
            })
            await send({
                "type": "http.response.body",
                "body": body
            })
            return

    await _real_app(scope, receive, send)

__all__ = ["app"]
