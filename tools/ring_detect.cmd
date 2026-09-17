@echo off
rem Launcher for tools/ring_detect.py (white ring inner/outer radius finder).
rem Click the ring center in the window; the tool then fits the white ring.
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ring] repo venv not found: %PY%
    pause
    exit /b 1
)
cd /d "%~dp0.."
"%PY%" "tools\ring_detect.py" %*
if errorlevel 1 pause
