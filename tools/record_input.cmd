@echo off
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0.."
".venv\Scripts\python.exe" "tools\record_input.py"
if errorlevel 1 pause
