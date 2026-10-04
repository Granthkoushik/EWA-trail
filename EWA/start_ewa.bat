@echo off
setlocal enabledelayedexpansion

title EWA - Enhanced Wearable Avatar Console
color 0b

echo ============================================================
echo        EWA: Enhanced Wearable Avatar Console
echo ============================================================
echo.

cd /d "%~dp0"

:: 1. Check Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in system PATH.
    echo Please install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

:: 2. Ensure .env exists
if not exist ".env" (
    if exist ".env.example" (
        echo [SETUP] Creating .env from template .env.example...
        copy /y ".env.example" ".env" >nul
        echo [SETUP] .env created. Remember to add your GEMINI_API_KEY in .env if available.
    )
)

:: 3. Check / Install dependencies
echo [SETUP] Checking Python dependencies...
python -m pip install -r requirements.txt --quiet --no-warn-script-location
if %errorlevel% neq 0 (
    echo [WARNING] Dependency install returned a non-zero code. Trying to continue...
)

:: 4. Create required runtime directories
if not exist "logs" mkdir "logs"
if not exist "audio" mkdir "audio"
if not exist "audio_debug" mkdir "audio_debug"

echo.
echo ============================================================
echo [STARTING] Launching EWA Server on http://localhost:5000 ...
echo ============================================================
echo.

:: 5. Launch browser automatically in background after a brief delay
start /b cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"

:: 6. Run ewa_server.py
python ewa_server.py

pause
