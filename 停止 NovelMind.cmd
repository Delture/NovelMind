@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [NovelMind] Missing .venv\Scripts\python.exe
    pause
    exit /b 1
)
".venv\Scripts\python.exe" "scripts\novelmind_launcher.py" --stop
if errorlevel 1 pause
endlocal
