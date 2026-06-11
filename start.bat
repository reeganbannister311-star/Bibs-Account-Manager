@echo off
title Bibs DreamBot Manager
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH.
    echo Make sure Python is installed and added to PATH.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
)

echo Installing dependencies...
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo Launching Bibs DreamBot Farm Manager...
.venv\Scripts\python src\main.py 2> debug.log
if errorlevel 1 (
    echo An error occurred. Check debug.log for details.
    type debug.log
)
pause
