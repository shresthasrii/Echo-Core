import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file
load_dotenv(BASE_DIR / ".env")

class Settings:
    PROJECT_NAME: str = "Echo-Core Incident Response Agent"
    VERSION: str = "2.0.0"
    BASE_DIR: Path = BASE_DIR
    
    # Hindsight Configuration
    HINDSIGHT_BASE_URL: str = os.getenv("HINDSIGHT_BASE_URL", "https://api.hindsight.vectorize.io")
    HINDSIGHT_API_KEY: str = os.getenv("HINDSIGHT_API_KEY", "")
    HINDSIGHT_BANK_ID: str = os.getenv("HINDSIGHT_BANK_ID", "acme-sre")
    
    # Groq Configuration
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    GROQ_FALLBACK_MODEL: str = os.getenv("GROQ_FALLBACK_MODEL", "qwen/qwen3.8-27b")
    GROQ_TERTIARY_MODEL: str = os.getenv("GROQ_TERTIARY_MODEL", "openai/gpt-oss-20b")
    
    # Server Configuration
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    
    # Paths with dynamic fallback for Vercel / serverless containers
    @property
    def SEED_DATA_PATH(self) -> Path:
        candidates = [
            BASE_DIR / "data" / "seed_incidents.json",
            BASE_DIR / "backend" / "seed_incidents.json",
            Path.cwd() / "data" / "seed_incidents.json",
            Path.cwd() / "backend" / "seed_incidents.json",
            Path("/var/task/data/seed_incidents.json"),
            Path("/var/task/backend/seed_incidents.json"),
            Path(__file__).resolve().parent / "seed_incidents.json",
        ]
        for c in candidates:
            if c.exists() and c.is_file():
                return c
        return BASE_DIR / "data" / "seed_incidents.json"

    @property
    def FRONTEND_DIR(self) -> Path:
        candidates = [
            BASE_DIR / "frontend",
            BASE_DIR / "backend",
            Path.cwd() / "frontend",
            Path.cwd() / "backend",
            Path("/var/task/frontend"),
            Path("/var/task/backend"),
            Path(__file__).resolve().parent,
        ]
        for c in candidates:
            if c.exists() and c.is_dir():
                return c
        return BASE_DIR / "frontend"

    @property
    def SQLITE_DB_PATH(self) -> Path:
        env_path = os.getenv("SQLITE_DB_PATH")
        if env_path:
            return Path(env_path)
        # Serverless / Vercel / AWS Lambda environment detection
        is_serverless = (
            bool(os.getenv("VERCEL"))
            or bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
            or os.path.exists("/var/task")
        )
        if is_serverless:
            return Path("/tmp") / "echo_core.db"
        
        # Test if local data directory is writable
        local_dir = BASE_DIR / "data"
        try:
            local_dir.mkdir(parents=True, exist_ok=True)
            test_file = local_dir / ".write_test"
            test_file.touch()
            test_file.unlink()
            return local_dir / "echo_core.db"
        except Exception:
            import tempfile
            return Path(tempfile.gettempdir()) / "echo_core.db"

settings = Settings()
