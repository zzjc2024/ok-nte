@echo off
rem Launcher for tools/record_process_audio.py.
rem Records the WASAPI *process* loopback (the same track SoundListener hears),
rem unlike record_game_audio.cmd which records the default output device.
rem Uses the repository .venv because the capture needs comtypes/numpy/ok.
rem No elevation by default; if the game itself runs as admin, run this elevated.
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [record] repo venv not found: %PY%
    pause
    exit /b 1
)
cd /d "%~dp0.."
"%PY%" "tools\record_process_audio.py" %*
if errorlevel 1 pause
