@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [NovelMind] Missing .venv\Scripts\python.exe
    echo Please finish the one-time environment setup in README.md first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" "scripts\novelmind_launcher.py"
if errorlevel 1 pause
endlocal
