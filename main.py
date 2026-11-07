"""
Echo-Core root entrypoint for Vercel, ASGI servers, and cloud deployments.
"""
from backend.main import app

if __name__ == "__main__":
    import uvicorn
    from backend.config import settings
    uvicorn.run("backend.main:app", host=settings.HOST, port=settings.PORT, reload=True)
