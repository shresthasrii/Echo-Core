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
    
    # Hindsight Configuration
    HINDSIGHT_BASE_URL: str = os.getenv("HINDSIGHT_BASE_URL", "https://api.hindsight.vectorize.io")
    HINDSIGHT_API_KEY: str = os.getenv("HINDSIGHT_API_KEY", "")
    HINDSIGHT_BANK_ID: str = os.getenv("HINDSIGHT_BANK_ID", "acme-sre")
    
    # Groq Configuration
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    GROQ_FALLBACK_MODEL: str = os.getenv("GROQ_FALLBACK_MODEL", "qwen/qwen3.8-27b")
    
    # Server Configuration
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    
    # Paths
    SEED_DATA_PATH: Path = BASE_DIR / "data" / "seed_incidents.json"
    FRONTEND_DIR: Path = BASE_DIR / "frontend"

settings = Settings()
