@echo off
rem Launcher for the game audio recorder (tools/game_recorder_gui.py).
rem Uses a dedicated tools venv OUTSIDE the repo, so the project .venv and the
rem global Python stay untouched.
rem
rem One-time setup (from the repo root):
rem   uv venv --python 3.12 "..\.venv-tools"
rem   uv pip install --python "..\.venv-tools\Scripts\python.exe" pyaudiowpatch keyboard
set "TOOLS_PY=%~dp0..\..\.venv-tools\Scripts\python.exe"
if not exist "%TOOLS_PY%" (
    echo [record] tools venv not found: %TOOLS_PY%
    echo [record] create it first:
    echo [record]   uv venv --python 3.12 "..\.venv-tools"
    echo [record]   uv pip install --python "..\.venv-tools\Scripts\python.exe" pyaudiowpatch keyboard
    pause
    exit /b 1
)
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
cd "logs"
echo [record] output: %CD%\game_audio_output.wav
"%TOOLS_PY%" "%~dp0game_recorder_gui.py"
if errorlevel 1 pause
