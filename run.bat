@echo off
setlocal enabledelayedexpansion

echo ====================================================================
echo                   Echo-Core Incident Response Agent
echo           Persistent Memory On-Call Demo (HackwithBengaluru 2.0)
echo ====================================================================
echo.

:: 1. Check Python installation
where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python was not found in PATH. Please install Python 3.10+ and try again.
    pause
    exit /b 1
)

:: 2. Check .env file
if not exist .env (
    if exist .env.example (
        echo [INFO] No .env found. Copying .env.example to .env...
        copy .env.example .env >nul
        echo [WARN] Created default .env. Please check API keys if needed.
    ) else (
        echo [ERROR] Neither .env nor .env.example found.
        pause
        exit /b 1
    )
)

:: 3. Check and install dependencies
echo [1/3] Verifying dependencies...
pip install -r requirements.txt --quiet

:: 4. Launch browser after a brief delay
echo [2/3] Launching Echo-Core Command Center at http://localhost:8000 ...
start "" http://localhost:8000/

:: 5. Start FastAPI + SSE Backend
echo [3/3] Starting Echo-Core Backend Server on 0.0.0.0:8000...
echo.
echo ====================================================================
echo  * Web UI:        http://localhost:8000/
echo  * SSE Feed:      http://localhost:8000/api/events
echo  * API Health:    http://localhost:8000/api/health
echo ====================================================================
echo.
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

pause
